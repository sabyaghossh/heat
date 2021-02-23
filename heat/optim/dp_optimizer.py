import inspect
import math
import torch
import torch.distributed
from torch.nn.parallel import DistributedDataParallel as tDDP
from typing import Union, List, Tuple, Dict

from ..core.communication import MPICommunication
from ..core.communication import MPI
from ..core.communication import MPI_WORLD
from .utils import DetectMetricPlateau


__all__ = ["DataParallelOptimizer", "DASO"]


def __sum_f16_cb(buffer_a, buffer_b, _):
    # MPI custom sum function to use torch.half
    tens_a = torch.HalfTensor().set_(torch.HalfStorage.from_buffer(buffer_a, "native"))
    tens_b = torch.HalfTensor().set_(torch.HalfStorage.from_buffer(buffer_b, "native"))
    tens_b += tens_a
    nelem = torch.prod(torch.tensor(tens_b.shape)).item()
    new_buff = MPI.memory.fromaddress(tens_b.data_ptr(), nbytes=tens_b.element_size() * nelem)
    buffer_b[:] = new_buff


def __sum_bfloat_cb(buffer_a, buffer_b, _):
    # MPI custom sum function to use torch.bfloat16
    tens_a = torch.BFloat16Tensor().set_(torch.BFloat16Storage.from_buffer(buffer_a, "native"))
    tens_b = torch.BFloat16Tensor().set_(torch.BFloat16Storage.from_buffer(buffer_b, "native"))
    tens_b += tens_a
    nelem = int(tens_b.numel())
    new_buff = MPI.memory.fromaddress(tens_b.data_ptr(), nbytes=nelem * tens_b.element_size())
    buffer_b[:] = new_buff


# create new MPI OPs
mpi_sum_f16 = MPI.Op.Create(__sum_f16_cb, commute=True)
mpi_sum_bfloat = MPI.Op.Create(__sum_bfloat_cb, commute=True)


class DASO:
    def __init__(
        self,
        local_optimizer: torch.optim.Optimizer,
        total_epochs: int,
        warmup_epochs: int = 4,
        cooldown_epochs: int = 4,
        scheduler: torch.optim.lr_scheduler = None,
        stability_level: float = 0.05,
        max_global_skips: int = 8,
        sending_chuck_size: int = 10_000_000,
        downcast_type: torch.dtype = torch.bfloat16,
        use_mpi_groups: bool = True,
        verbose: bool = False,
    ):
        """
        Optimizer wrapper to use the Distributed Asynchronous and Selective Optimization (DASO) method.

        This optimizer uses a local torch optimizer combined with the :class:`..nn.data_parappel.DataParallelMultiGPU`
        to create local DPNNs on each node consisting of the GPUs on each node. Then those networks communicate
        globally with MPI groups, each of which has a single GPU on each node.

        DASO uses both local and global synchronization operations. Local synchronization operations are intended to be
        done very frequently while global synchronizations are conducted asynchronously as the next batches are
        computed.

        There are four phases to training:
            1. initialization (steps 1 to 8 above)
            2. Training: warmup phase
                - blocking averaging update occurs for global synchronization step
            3. Training: cycling phase
                - for the global synchronization, the data is sent after a number of batches. the number of batches
                between synchronizations is referred to as `global_skips`. After the data is sent a number of batches
                pass before it is received (`batches_to_wait`). both of these cycle downward from `max_global_skips`
                for the global skips and 1/4th this value for `batches_to_wait`. When both values are equal to 1 and
                the loss is stable it will be reset to the initial values, then will decay again.
            4. Training: cooldown phase
                - blocking averaging update occurs for global synchronization step

        As example usage of this can be found in :file:`../../examples/nn/imagenet-DASO.py`.

        The recommended checklist for using this class is as follows:
            1. initialize the local PyTorch process group and set the default device of the local GPUs.
            2. define the torch network
            3. define the `local_optimizer` -> a torch optimizer of your choice (tested with SGD)
            4. (optional) choose a learning rate scheduler -- this is only for those learning rates which will
            also step the optimizer
            5. initialize DASO with the local optimizers and parameters
            6. initialize :class:`../nn/DataParallelMultiGPU` with the torch network and DASO
            7. If using automatic mixed precision (:class:`torch.cuda.amp`), initialize the gradient scaler and add it to DASO (:func:`add_scaler`)
            8. ensure that the DataLoaders evenly distribute the data between all the processes
                - this can be done by using the :class:`torch.utils.data.distributed.DistributedSampler` with the `num_replicas` and `rank` parameters
            9. call `daso_optimizer.epoch_loss_logic(training_loss)` at the end of
            10. set the number of batches per epoch (`daso_optimizer.last_batch = number_of_batches`)
            11. ensure that the step function used in training is that of the DASO optimizer

        Parameters
        ----------
        local_optimizer: torch.optim.Optimizer
            This optimizer handles the optimization of the local NN. Example: torch.optim.SGD.
            This can be any optimizer, although tests were only completed with SGD. Other optimizers may show
            unexpected behavior
        total_epochs: int
            The total number of epochs for training. Needed to determine when to enter the cooldown phase
        warmup_epochs: int, optional
            The number of epochs to complete with a blocking averaging operation after each batch before entering
            the cycling phase.
            Default: 4
        cooldown_epochs: int, optional
            The number of epochs with blocking averaging operations after each batch at the end of training.
            Default: 4
        scheduler: torch.optim.lr_scheduler, optional
            Local PyTorch learning rate scheduler. This must be used in the case that the scheduler's `step` function
            is supposed to be called instead of the optimizer's `step` function.
            Default: None
        stability_level: float, optional
            This can be viewed as the percent change threshold that the loss must exceed to be judged as improving.
            When the loss is within this percent change for 2 epochs, then it is judged as stable.
            Default: 0.05
        max_global_skips: int, optional
            The maximum number of batches between the beginning of a global synchronization process
            Default: 8
        sending_chuck_size: int, optional
            During the global synchronization step, the network parameters are split into chunks of data to overlap
            communication and computation. This value is the maximum chunk size.
            Default: 10_000_000
        downcast_type: torch.dtype, optional
            When the network parameters are sent during the global synchronization step, they are cast down to
            a smaller dtype, by default this is `torch.bfloat16`. Smaller torch dtypes are not implemented.
            torch.bfloat16
        use_mpi_groups: bool, optional
            Use MPI groups to divide the global communicator. If True, use MPI GROUPs, otherwise, use MPI SPLIT.
            Default: True
        verbose: bool, optional
            If true, print out a collection of debug messages
            Default: False
        """
        # check dtypes
        frame = inspect.currentframe()
        init_args = inspect.getargvalues(frame)[3]
        self.__init_checktypes(init_args)

        if downcast_type == torch.bfloat16:
            self.cast_fn = mpi_sum_bfloat
        elif downcast_type == torch.half:
            self.cast_fn = mpi_sum_f16
        else:
            raise ValueError(
                f"downcast_type must be one of torch.bfloat16 or torch.half, currently {downcast_type}"
            )

        self.comm = MPI_WORLD
        self.verbose = verbose
        self.local_optimizer = local_optimizer
        self.params_ref = local_optimizer.param_groups[0]["params"]
        # reference of optimizer's params
        self.scheduler = scheduler

        rank = MPI_WORLD.rank
        loc_gpus = torch.cuda.device_count()
        self.loc_gpus = loc_gpus
        local_rank = rank % loc_gpus
        self.local_skip = 1
        if loc_gpus > 1:
            base_loc_ranks = list(range(0, MPI_WORLD.size, loc_gpus))
            reduced_comms, reduced_ranks = [], []
            for i in range(loc_gpus):
                lp_ranks = [j + i for j in base_loc_ranks]
                if use_mpi_groups:
                    new_group = MPI_WORLD.group.Incl(lp_ranks)
                    new_comm = MPI_WORLD.Create_group(new_group)
                    reduced_comms.append(MPICommunication(new_comm, group=True))
                else:
                    color = 111 + i if rank in lp_ranks else 222 + i
                    key = 0 + i if rank in lp_ranks else 444 + i
                    reduced_comms.append(MPICommunication(MPI_WORLD.Split(color, key)))
                reduced_ranks.append(tuple(lp_ranks))
            self.reduced_comms, self.reduced_ranks = reduced_comms, reduced_ranks
            self.base_loc_ranks = base_loc_ranks
            if loc_gpus != torch.cuda.device_count():
                self.device = "cuda:0"
            else:
                self.device = "cuda:" + str(local_rank)
            torch.cuda.set_device(device=self.device)

        self.current_batch, self.last_batch = 0, None

        self._prev_params = []
        self.epoch = 0
        self._send_mod, self._send_mod_m1 = 0, None

        self.global_skip = 0
        self.local_skip = 0
        self.batches_to_wait = 0
        self.epochs_to_wait = 3
        self.max_gs = max_global_skips

        self.warmup_epochs = warmup_epochs
        self.cooldown_epochs = cooldown_epochs
        self.total_epochs = total_epochs

        # used in the sending of the params
        self._param_send_buffer_size = None
        self.param_dict, self.shapes = None, None
        self._param_send_shp = None
        self.split = None

        self.stability = DetectMetricPlateau(patience=2, threshold=stability_level)

        self._gs8_waits = 3
        self._gs8_waited = 0

        self.split_val = sending_chuck_size

        # TODO: its possible that the split indexes could be used to avoid the concatenating method used currently
        self.split_inds = None
        self.amp = False

    def add_scaler(self, scaler: torch.cuda.amp.GradScaler):
        """
        Create a reference to torch's :class:`torch.cuda.amp.GradScaler` used in torch's automatic mixed
        precision. For more information on this see https://pytorch.org/docs/stable/notes/amp_examples.html

        Parameters
        ----------
        scaler: torch.cuda.amp.GradScaler
            the gradient scaler to be used
        """
        self.scaler = scaler
        self.amp = True

    @staticmethod
    def __init_checktypes(args):
        # this does all of the checks and raises for the parameters for init
        if not isinstance(args["local_optimizer"], torch.optim.Optimizer):
            raise TypeError(
                f"Local optimizer must be a torch optimizer object, currently {type(args['local_optimizer'])}"
            )
        if not isinstance(args["total_epochs"], int):
            raise TypeError(f"total_epochs must be an int, currently {type(args['total_epochs'])}")
        if not isinstance(args["warmup_epochs"], int):
            raise TypeError(
                f"warmup_epochs must be an int, currently {type(args['warmup_epochs'])}"
            )
        if not isinstance(args["cooldown_epochs"], int):
            raise TypeError(
                f"cooldown_epochs must be an int, currently {type(args['cooldown_epochs'])}"
            )
        if args["scheduler"] is not None and not issubclass(
            args["scheduler"], torch.optim.lr_scheduler._LRScheduler
        ):
            raise TypeError(
                f"scheduler must be a torch learning rate scheduler, currently {args['scheduler']}"
            )
        if not isinstance(args["stability_level"], float):
            raise TypeError(
                f"stablitiy_level must be a float, currently {type(args['stablitiy_level'])}"
            )
        if not isinstance(args["max_global_skips"], int):
            raise TypeError(
                f"max_global_skips must be an int, currently {type(args['max_global_skips'])}"
            )
        if not isinstance(args["sending_chuck_size"], int):
            raise TypeError(
                f"sending_chuck_size must be an int, currently {type(args['sending_chuck_size'])}"
            )
        if not isinstance(args["verbose"], bool):
            raise TypeError(f"verbose must be a bool, currently {type(args['verbose'])}")
        if not isinstance(args["use_mpi_groups"], bool):
            raise TypeError(
                f"`use_mpi_grus` must be a bool, currently {type(args['use_mpi_groups'])}"
            )
        if not isinstance(args["downcast_type"], torch.dtype):
            raise TypeError(
                f"downcast_type must be a torch.dtype, currently {args['downcast_type']}"
            )
        if args["warmup_epochs"] < 0:
            raise ValueError(f"warmup_epochs must be >= 0")
        if args["cooldown_epochs"] < 0:
            raise ValueError(f"cooldown_epochs must be >= 0")
        if args["max_global_skips"] < 0:
            raise ValueError(f"stablitiy_level must be >= 0")
        if args["sending_chuck_size"] <= 0:
            raise ValueError(f"sending_chuck_size must be > 0")
        if args["total_epochs"] <= 0:
            raise ValueError(f"total_epochs must be > 0")

    @torch.no_grad()
    def epoch_loss_logic(self, loss, loss_globally_averaged=False):
        """
        Function controlling the number of batches between global synchronizations and the batches to wait before
        receiving the sent parameters. The warm-up and cool-down phases are also controlled here.

        This function should be called at the end of each epoch with the training loss value at the end of the epoch.

        The number of batches between local synchronizations can also be modified here with minor code adjustments.

        Parameters
        ----------
        loss: torch.Tensor or float
            loss value of the current epoch
        loss_globally_averaged: bool, optional
            boolean if the loss is already globally averaged
        """

        if not loss_globally_averaged:
            loss_send = torch.zeros(self.comm.size)
            # loss.data -> this will get the raw number from the lass value and nothing else
            loss_send[self.comm.rank] = loss.data if isinstance(loss, torch.Tensor) else loss
            self.comm.Allreduce(MPI.IN_PLACE, loss_send, MPI.SUM)
            avg_loss = torch.mean(loss_send)
        else:
            avg_loss = torch.tensor(loss)

        if self.epoch < self.warmup_epochs:
            self.global_skip = 0
            self.local_skip = 0
            self.batches_to_wait = 0

            self.print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)
            return

        elif self.warmup_epochs == self.epoch:
            self.global_skip = 4
            self.local_skip = 1
            self.batches_to_wait = 1

            self.print0("\t\t", self.global_skip, self.local_skip, self.batches_to_wait)

        if self.epoch >= self.total_epochs - self.cooldown_epochs:
            self.global_skip = 0
            self.local_skip = 0
            self.batches_to_wait = 0

            self.print0(
                "\tGlobal Skips:",
                self.global_skip,
                "Local Skips:",
                self.local_skip,
                "Batches to wait:",
                self.batches_to_wait,
            )
            return

        if self.global_skip == self.max_gs and self.max_gs > 4:
            self._gs8_waited += 1

        self.print0(
            "current best:",
            self.stability.best * (1.0 - self.stability.threshold),
            "avg loss:",
            avg_loss,
            "bad epochs:",
            self.stability.num_bad_epochs,
        )

        stable = self.stability.test_if_improving(avg_loss)

        if stable and self.global_skip > 1:
            # drop gs by factor of 2
            self.global_skip //= 2
            self.local_skip //= 2
            self.batches_to_wait -= 1  # old was //= 2

            self.print0("dropping skips")

            if self.global_skip > 0:
                if self.batches_to_wait == 0:
                    self.batches_to_wait = 1
                if self.local_skip == 0:
                    self.local_skip = 1
            self._gs8_waited = 0
        elif self.global_skip == 1 and stable:
            self.global_skip = self.max_gs
            self.local_skip = self.max_gs // 4
            self.batches_to_wait = self.max_gs // 4  # + 1  # 2

            self._gs8_waited = 0

        self.print0(
            "\tGlobal Skips:",
            self.global_skip,
            "Local Skips:",
            self.local_skip,
            "Batches to wait:",
            self.batches_to_wait,
            "\nAvg loss:",
            avg_loss,
            "Number of bad Epochs",
            self.stability.num_bad_epochs,
        )

    @torch.no_grad()
    def _global_sync(self, batches_to_wait):
        """
        Performs a global synchronization. If `batches_to_wait > 0` this will wait for that many
        batches before received in the parameters.

        Full syncs are only performed on a single MPI group
        """
        current_comm = self.reduced_comms[self._send_mod]
        current_ranks = self.reduced_ranks[self._send_mod]

        if self.comm.rank in current_ranks:
            self._gs_send_params(current_comm, batches_to_wait)

        if self.batches_to_wait != 0:
            # update parameters from the last sending (if there)
            self._gs_rcv_update_params()  # -> splits off irrelevant ranks
            # needs to happen on all ranks:
            self._local_update(self._send_mod_m1)

        if self.current_batch != self.last_batch and self.batches_to_wait != 0:
            self.current_batch += 1
            self._send_mod_m1 = self._send_mod
            self._send_mod = self._send_mod + 1 if self._send_mod <= self.loc_gpus - 2 else 0
            return

        # if self.current_batch == self.last_batch or self.batches_to_wait == 0:
        #    this will only run if the batch is the last of the epoch,
        #    or the most recently sent data is to be immediately received (batches_to_wait == 0)
        #    -> receive the sent data to sync params across all ranks
        if self.comm.rank in current_ranks:
            self._gs_rcv_update_params_last_batch(current_ranks)
        else:
            if len(self._prev_params) > 0:
                raise ValueError(
                    f"DEBUG: off ranks have data when they shouldn't, {len(self._prev_params)}"
                    f" batch number {self.current_batch}"
                )
        self._local_update(self._send_mod)

        self._send_mod_m1 = None

        if self.current_batch == self.last_batch:
            self._send_mod = 0
            self.epoch += 1
            self.current_batch = 0
        else:
            self.current_batch += 1
            self._send_mod = self._send_mod + 1 if self._send_mod <= self.loc_gpus - 2 else 0

    def _gs_create_param_dict(self):
        """
        create the shape and param dictionary used for sending parameters around the MPI world.
        this will also define the buffer size if it was not previously defined.
        """
        if self.shapes is not None:
            return self.param_dict, self.shapes
        param_dict = {}
        shapes = {}
        st = 0
        for name, param in self.module.named_parameters():
            param_dict[name] = param
            numel = param.numel()
            shapes[name] = [param.shape, slice(st, st + numel), param.dtype]
            st += numel

        if self._param_send_buffer_size is None:
            # use the total number of elements to define the sending buffer shape (single int)
            self._param_send_buffer_size = st

        self.param_dict = param_dict
        self.shapes = shapes
        return param_dict, shapes

    @torch.no_grad()
    def _gs_rcv_update_params(self):
        """
        receive the previously sent parameters for the last sending MPI group.
        this is also where the sent and local parameters are merged together.
        """
        # wait for the global sync data and update on the selected rank,
        if self._send_mod_m1 is None:
            return
        prev_ranks = self.reduced_ranks[self._send_mod_m1]
        if self.comm.rank not in prev_ranks or len(self._prev_params) == 0:
            # if no old gradients, return without doing anything
            return

        prev_params = self._prev_params.pop(0)
        batches_between = float(prev_params[3])
        shapes = prev_params[2]
        # add the weighted average to the received params
        numer = batches_between * 2.0 if batches_between > 0.0 else 1.0
        denom = float(len(prev_ranks) + numer)
        factor = numer / denom
        if not self.split:
            # only a single buffer
            prev_params[0].Wait()
            rcv_params = prev_params[1] / denom
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    update = (
                        rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                    )
                    # NOTE: update here is the sum of the params across the processes
                    param *= factor
                    param += update
        else:
            # receive the first buffer
            ind = 0
            prev_params[0][0].Wait()
            del prev_params[0][0]
            rcv_params = prev_params[1][ind] / denom
            # jit the parameter setting
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    if shapes[name][1].stop > len(rcv_params):
                        # when the end of the slice is higher than the amount received, wait for the next buffer
                        ind += 1
                        prev_params[0][0].Wait()
                        del prev_params[0][0]
                        new_rcv_params = prev_params[1][ind] / denom
                        rcv_params = torch.cat((rcv_params, new_rcv_params))
                    update = (
                        rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                    )
                    # NOTE: update here is the sum of the params across the processes
                    param *= factor
                    param += update

    @torch.no_grad()
    def _gs_rcv_update_params_last_batch(self, current_ranks):
        """
        abstracted receive for the last batch (and if `global_skips` == 0)
        """
        if len(self._prev_params) > 1:
            raise ValueError(f"length of previous params > 1! {len(self._prev_params)}")
        prev_params = self._prev_params.pop(0)
        shapes = prev_params[2]
        if not self.split:
            # print("before wait")
            prev_params[0].Wait()
            rcv_params = prev_params[1] / float(len(current_ranks))
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    param[:] = (
                        rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                    )
        else:
            ind1 = 0
            prev_params[0][0].Wait()
            del prev_params[0][0]
            rcv_params = prev_params[1][ind1] / float(len(current_ranks))
            for name, param in self.module.named_parameters():
                if param.requires_grad:
                    while shapes[name][1].stop > len(rcv_params):
                        ind1 += 1
                        prev_params[0][0].Wait()
                        del prev_params[0][0]
                        new_rcv_params = prev_params[1][ind1] / float(len(current_ranks))
                        rcv_params = torch.cat((rcv_params, new_rcv_params))
                    param[:] = (
                        rcv_params[shapes[name][1]].reshape(shapes[name][0]).to(shapes[name][2])
                    )
        if len(self._prev_params) >= 1:
            print("len of prev_params is > 1!", self._prev_params)

    @torch.no_grad()
    def _gs_send_params(self, current_comm, batches_to_wait):
        """
        pack and send the data required for a global synchronization on the `current_comm` group

        `batches_to_wait` is sent with the parameters to keep track of this between sending and receiving
        """
        op = MPI.SUM
        cast = False
        if self.global_skip < 1:
            op = mpi_sum_bfloat
            cast = True

        param_dict, shapes = self._gs_create_param_dict()
        sndparams = torch.zeros(
            self._param_send_buffer_size, device=self.device, dtype=torch.bfloat16 if cast else None
        )

        sndparams = self.__pack_data(sndparams, param_dict, cast)

        # if sndparams.isnan().sum():
        #     # check if there are NaNs, if so, stuff is bad
        #     raise ValueError(f"{sndparams.isnan().sum()} NaNs in `params` shit be fucked.")

        if not self.split and sndparams.numel() <= self.split_val:
            new_wait = current_comm.Iallreduce(MPI.IN_PLACE, sndparams, op)
            self._prev_params.append([new_wait, sndparams, shapes, batches_to_wait])
            return

        # if self.split or sndparams.numel() > self.split_val:
        self.split = True
        num_splits = math.ceil(len(sndparams) / self.split_val)
        splits = [self.split_val] * (num_splits - 1)
        rem = len(sndparams) - (self.split_val * (num_splits - 1))
        # first one will be smaller then the rest (the remainder is first)
        splits = [rem] + splits
        self.split_inds = splits
        params_list = [None] * num_splits
        prev = 0
        waits = [None] * num_splits
        for s in range(num_splits):
            # need to slice the params at the split points
            params_list[s] = sndparams[prev : splits[s] + prev]
            prev += splits[s]
            waits[s] = current_comm.Iallreduce(MPI.IN_PLACE, params_list[s], op)
        self._prev_params.append([waits, params_list, shapes, batches_to_wait])

    @torch.no_grad()
    def _local_update(self, sending_process):
        # use torch to send the network parameters of a single process to the other processes
        if not torch.distributed.is_initialized() or sending_process is None:
            return
        snds = {}
        for name, param in self.module.named_parameters():
            if param.requires_grad:
                snds[name] = torch.distributed.broadcast(
                    param, sending_process, async_op=True
                )  # default is SUM
        for name, param in self.module.named_parameters():
            if param.requires_grad:
                snds[name].wait()

    @staticmethod
    @torch.no_grad()
    @torch.jit.script
    def __pack_data(jtparams: torch.Tensor, iter_dict: Dict[str, torch.Tensor], cast: bool):
        """ jitted loop to pack the data into a flattened buffer to be sent"""
        st = 0
        for name, par in iter_dict.items():
            if par.requires_grad:
                # flatten and prep the data for sending
                p = torch.flatten(par)
                if cast:
                    p = p.to(torch.bfloat16)
                jtparams[st : st + par.numel()] = p
                st += par.numel()
        return jtparams

    def print0(self, *args, **kwargs):
        """
        Print a message on rank 0 if the class parameter `verbose` is set.
        """
        if MPI_WORLD.rank == 0 and self.verbose:
            print(*args, **kwargs)

    def set_model(self, model: torch.nn.Module):
        """
        Set the local model for the optimizer.
        This should be called by the :class:`..nn.data_parappel.DataParallelMultiGPU`.

        Parameters
        ----------
        model: torch.nn.Module
            the local torch model.
        """
        self.module = model

    def _start_local_sync(self):
        # *start* local synchronizations for the next batches
        if not isinstance(self.module, tDDP) or self.module.require_backward_grad_sync:
            # this has no effect if the module is not locally distributed in torch
            return
        self.module.require_backward_grad_sync = True

    def step(self):
        """
        Perform a single optimization step.
        This will perform the `step` operations of the local optimizer,
        local learning rate scheduler (if defined), and the gradient scaler used in automatic mixed
        precision (if defined).

        Also in the step is the logic used for when to send and receive the global/local synchronizations.
        Global Syncs occur on batches for which the modulus of the batch number and the `global_skip` number is 0.
        If `batches_to_wait` > 0, the next batches have only local syncs. After that number of batches,
        the data during the global sync phase is received.

        Local synchronization can also be turned off if desired by increasing `local_skips` above 1.

        Notes
        -----
        self.last_batch must be set!
        """
        if self.last_batch is None:
            raise ValueError(
                "self.last_batch must be set as the number of batches (len(dataloader))"
            )

        if self.amp:
            self.scaler.step(self.local_optimizer)
            # todo: add something to tell if the grads have infs or nans
            # Updates the scale for next iteration.
            self.scaler.update()
        elif self.scheduler is None:
            self.local_optimizer.step()
        else:
            self.scheduler.step()
        batch = self.current_batch
        # knowing next_batch is important to make sure that the local sync is on
        #       or if the next is the last batch
        next_batch = batch + 1
        gs = self.global_skip
        ls = self.local_skip
        # determine if to do the syncs
        gmod = batch % gs if gs > 0 else 0
        lmod = batch % ls if ls > 0 else 0

        batches_to_wait = self.batches_to_wait
        # ensure that the batch that will receive will be before the end of the training loop
        btw = (
            batches_to_wait
            if batches_to_wait + batch <= self.last_batch
            else self.last_batch - batch
        )
        # do full sync on global skips and on the last batch
        if batch == self.last_batch or gmod == 0:
            return self._global_sync(btw)

        if next_batch % gs == 0:
            self._start_local_sync()
            self.current_batch += 1
            return

        if gmod < btw:
            # do nothing on these batches (maintain the local sync)
            self.current_batch += 1
            if next_batch == self.last_batch:
                self._start_local_sync()
            return
        elif gmod == btw:
            # local updates should be on before this is called!
            self._gs_rcv_update_params()
            self._local_update(self._send_mod_m1)
            if ls > 1:
                self._stop_local_sync()

        if ls == 1 and next_batch != self.last_batch:
            self.current_batch += 1
            self._start_local_sync()
            return

        if lmod == 0:
            self._stop_local_sync()
        elif next_batch % ls == 0:
            self._start_local_sync()

        if next_batch == self.last_batch:
            self._start_local_sync()

        self.current_batch += 1

    def _stop_local_sync(self):
        # stop local synchronizations for the next batches
        if not isinstance(self.module, tDDP) or not self.module.require_backward_grad_sync:
            # this has no effect if the module is not locally distributed in torch
            return
        self.module.require_backward_grad_sync = False

    def zero_grad(self) -> None:
        """
        Reset gradients of local optimizer's params.
        """
        # reset view onto params in order to reset all gradients
        self.local_optimizer.param_groups[0]["params"] = self.params_ref[:]
        self.local_optimizer.zero_grad()


class DataParallelOptimizer:
    """
    Uses a Torch.optim.Optimizer for data parallelism. It should be used in combination with DataParallel (DP) class.
    To optimize a DP module, DP optimizer has to be passed to DP module during its initialization.
    See :func:`..nn.DataParallel` for a basic example of usage.

    Attributes
    ----------
    torch_optimizer : torch.optim.Optimizer
        the wrapped Torch optimizer
    blocking : bool
        use blocking communications or not. will typically be overwritten by heat.nn.DataParallel
    """

    def __init__(self, torch_optimizer: torch.optim.Optimizer, blocking: bool = False):
        self.torch_optimizer = torch_optimizer
        if not isinstance(blocking, bool):
            raise TypeError(f"blocking parameter must be a boolean, currently {type(blocking)}")
        # flag indicating if communication during parameter updates is blocking.
        self.blocking_parameter_updates = blocking
        # flag indicating if optimizer should take a step during next iteration (only relevant for non-blocking)
        self.update_next = False
        # reference of optimizer's params
        self.params_ref = torch_optimizer.param_groups[0]["params"]

    def step(self) -> None:
        """
        Force torch optimizer to update model parameters. For blocking, optimizer immediately updates parameters. For
        non-blocking, optimizer will update parameters during next forward.
        """
        if self.blocking_parameter_updates:
            self.torch_optimizer.step()
        else:
            self.update_next = True

    def zero_grad(self) -> None:
        """
        Reset gradients of optimizer's params.
        """
        # reset view onto params in order to reset all gradients
        self.torch_optimizer.param_groups[0]["params"] = self.params_ref[:]
        self.torch_optimizer.zero_grad()
