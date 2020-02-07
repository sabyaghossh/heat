import unittest
import os

import torch

import heat as ht
import numpy as np
import math

if os.environ.get("DEVICE") == "gpu" and torch.cuda.is_available():
    ht.use_device("gpu")
    torch.cuda.set_device(torch.device(ht.get_device().torch_device))
else:
    ht.use_device("cpu")
device = ht.get_device().torch_device
ht_device = None
if os.environ.get("DEVICE") == "lgpu" and torch.cuda.is_available():
    device = ht.gpu.torch_device
    ht_device = ht.gpu
    torch.cuda.set_device(device)


class TestDistances(unittest.TestCase):
    def test_cdist(self):
        X = ht.ones((4, 4), dtype=ht.float32, split=None)
        Y = ht.zeros((4, 4), dtype=ht.float32, split=None)
        res_XX_cdist = ht.zeros((4, 4), dtype=ht.float32, split=None)
        res_XX_rbf = ht.ones((4, 4), dtype=ht.float32, split=None)
        res_XY_cdist = ht.ones((4, 4), dtype=ht.float32, split=None) * 2
        res_XY_rbf = ht.ones((4, 4), dtype=ht.float32, split=None) * math.exp(-1.0)

        # Case 1a: X.split == None, Y == None
        d = ht.spatial.cdist(X, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XX_cdist))
        self.assertEqual(d.split, None)

        d = ht.spatial.cdist(X, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XX_cdist))
        self.assertEqual(d.split, None)

        d = ht.spatial.rbf(X, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XX_rbf))
        self.assertEqual(d.split, None)

        d = ht.spatial.rbf(X, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XX_rbf))
        self.assertEqual(d.split, None)

        # Case 1b: X.split == None, Y != None, Y.split == None
        d = ht.spatial.cdist(X, Y, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, None)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, None)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, None)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, None)

        # Case 1c: X.split == None, Y != None, Y.split == 0
        Y.resplit_(axis=0)
        res_XX_cdist.resplit_(axis=1)
        res_XX_rbf.resplit_(axis=1)
        res_XY_cdist.resplit_(axis=1)
        res_XY_rbf.resplit_(axis=1)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 1)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 1)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 1)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 1)

        # Case 2a: X.split == 0, Y == None
        X.resplit_(axis=0)
        Y.resplit_(axis=None)
        res_XX_cdist.resplit_(axis=0)
        res_XX_rbf.resplit_(axis=0)
        res_XY_cdist.resplit_(axis=0)
        res_XY_rbf.resplit_(axis=0)

        d = ht.spatial.cdist(X, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XX_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.cdist(X, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XX_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XX_rbf))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XX_rbf))
        self.assertEqual(d.split, 0)

        # Case 2b: X.split == 0, Y != None, Y.split == None
        d = ht.spatial.cdist(X, Y, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 0)

        # Case 2c: X.split == 0, Y != None, Y.split == 0
        Y.resplit_(axis=0)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.cdist(X, Y, quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_cdist))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=False)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 0)

        d = ht.spatial.rbf(X, Y, sigma=math.sqrt(2.0), quadratic_expansion=True)
        self.assertTrue(ht.equal(d, res_XY_rbf))
        self.assertEqual(d.split, 0)

        # Case 3 X.split == 1
        X.resplit_(axis=1)
        with self.assertRaises(NotImplementedError):
            d = ht.spatial.cdist(X)
        with self.assertRaises(NotImplementedError):
            d = ht.spatial.cdist(X, Y, quadratic_expansion=False)

        n = ht.communication.MPI_WORLD.size
        A = ht.ones((n * 2, 6), dtype=ht.float32, split=None)
        for i in range(n):
            A[2 * i, :] = A[2 * i, :] * (2 * i)
            A[2 * i + 1, :] = A[2 * i + 1, :] * (2 * i + 1)
        res = torch.cdist(A._DNDarray__array, A._DNDarray__array)

        A.resplit_(axis=0)
        B = A.astype(ht.int32)

        d = ht.spatial.cdist(A, B, quadratic_expansion=False)
        result = ht.array(res, dtype=ht.float64, split=0)
        self.assertTrue(ht.allclose(d, result, atol=1e-8))
