import contextlib
import gc
import sys
import math
import torch
import unittest
import warnings
import random
from copy import deepcopy
from collections import OrderedDict
from itertools import product
from operator import mul
from functools import reduce
import torch.nn.functional as F
from torch.autograd.gradcheck import gradgradcheck, gradcheck
from torch.autograd.function import once_differentiable
from torch.autograd.profiler import profile

from common import TestCase, run_tests, skipIfNoLapack
from torch.autograd._functions import *
from torch.autograd import Variable, Function

if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle

PRECISION = 1e-4


@contextlib.contextmanager
def backward_engine(engine):
    _prev_engine = Variable._execution_engine
    Variable._execution_engine = engine()
    try:
        yield
    finally:
        Variable._execution_engine = _prev_engine


def graph_desc(fn):
    if fn is None:
        return 'None'
    result = type(fn).__name__ + '('
    next_functions = fn.next_functions
    for next_fn, _ in next_functions:
        result += graph_desc(next_fn)
        result += ', '
    if next_functions:
        result = result[:-2]
    return result + ')'


class TestAutograd(TestCase):

    def _function_test(self, cls):
        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=True)
        result = cls.apply(x, 2, y)
        go = Variable(torch.ones(1), requires_grad=True)
        result.sum().backward(go)

        self.assertEqual(x.grad.data, y.data + torch.ones(5, 5))
        self.assertEqual(y.grad.data, x.data + torch.ones(5, 5) * 2)

        self.assertFalse(x.grad.volatile)
        self.assertFalse(y.grad.volatile)
        self.assertIsNotNone(x.grad.grad_fn)
        self.assertIsNotNone(y.grad.grad_fn)

        return x, y

    def test_function(self):
        class MyFunction(Function):

            @staticmethod
            def forward(ctx, tensor1, scalar, tensor2):
                ctx.scalar = scalar
                ctx.save_for_backward(tensor1, tensor2)
                return tensor1 + scalar * tensor2 + tensor1 * tensor2

            @staticmethod
            def backward(ctx, grad_output):
                var1, var2 = ctx.saved_variables
                # NOTE: self is the test case here
                self.assertIsInstance(var1, Variable)
                self.assertIsInstance(var2, Variable)
                self.assertIsInstance(grad_output, Variable)
                return (grad_output + grad_output * var2, None,
                        grad_output * ctx.scalar + grad_output * var1)

        x, y = self._function_test(MyFunction)

        x_grad_desc = graph_desc(x.grad.grad_fn)
        y_grad_desc = graph_desc(y.grad.grad_fn)
        self.assertEqual(
            x_grad_desc,
            'Identity(AddBackward1(ExpandBackward(AccumulateGrad()), '
            'MulBackward1(ExpandBackward(AccumulateGrad()), AccumulateGrad())))')
        self.assertEqual(
            y_grad_desc,
            'Identity(AddBackward1(MulBackward0(ExpandBackward(AccumulateGrad())), '
            'MulBackward1(ExpandBackward(AccumulateGrad()), AccumulateGrad())))')

    def test_once_differentiable(self):
        class MyFunction(Function):

            @staticmethod
            def forward(ctx, tensor1, scalar, tensor2):
                ctx.scalar = scalar
                ctx.save_for_backward(tensor1, tensor2)
                return tensor1 + scalar * tensor2 + tensor1 * tensor2

            @staticmethod
            @once_differentiable
            def backward(ctx, grad_output):
                t1, t2 = ctx.saved_tensors
                # NOTE: self is the test case here
                self.assertTrue(torch.is_tensor(t1))
                self.assertTrue(torch.is_tensor(t2))
                self.assertTrue(torch.is_tensor(grad_output))
                return (grad_output + grad_output * t2, None,
                        grad_output * ctx.scalar + grad_output * t1)

        x, y = self._function_test(MyFunction)
        self.assertEqual(graph_desc(x.grad.grad_fn),
                         'Identity(Error(AccumulateGrad(), None, AccumulateGrad()))')
        self.assertEqual(graph_desc(y.grad.grad_fn),
                         'Identity(Error(AccumulateGrad(), None, AccumulateGrad()))')

    def test_accumulate_grad(self):
        grad_output = Variable(torch.ones(5, 5))
        for start_volatile, end_volatile in product((True, False), repeat=2):
            go1 = grad_output.data if start_volatile else grad_output
            go2 = grad_output.data if end_volatile else grad_output

            x = Variable(torch.randn(5, 5), requires_grad=True)
            y = x + 2
            y.backward(go1, retain_graph=True)
            x_grad = x.grad
            x_grad_clone = x.grad.data.clone()
            y.backward(go2)

            # That's the only case when we can accumulate in-place
            # TODO: reconsider this logic (see accumulate_grad.cpp)
            if start_volatile:
                expected_grad = x_grad_clone * 2
            else:
                expected_grad = x_grad_clone
            self.assertEqual(x_grad.data, expected_grad)

    def test_hessian_vector(self):
        x = Variable(torch.randn(2, 2), requires_grad=True)
        y = Variable(torch.randn(2, 2), requires_grad=True)

        z = x ** 2 + y * x + y ** 2
        z.backward(Variable(torch.ones(2, 2), requires_grad=True), retain_graph=True)

        x_grad = 2 * x.data + y.data
        y_grad = x.data + 2 * y.data
        self.assertEqual(x.grad.data, x_grad)
        self.assertEqual(y.grad.data, y_grad)

        grad_sum = 2 * x.grad + y.grad
        grad_sum.backward(torch.ones(2, 2))
        x_hv = torch.ones(2, 2) * 5
        y_hv = torch.ones(2, 2) * 4
        self.assertEqual(x.grad.data, x_grad + x_hv)
        self.assertEqual(y.grad.data, y_grad + y_hv)

    def test_grad(self):
        x = Variable(torch.randn(2, 2), requires_grad=True)
        y = Variable(torch.randn(2, 2), requires_grad=True)
        z = x ** 2 + y * x + y ** 2
        z.backward(Variable(torch.ones(2, 2)), retain_graph=True)

        x_grad = 2 * x.data + y.data
        y_grad = x.data + 2 * y.data
        self.assertEqual(x.grad.data, x_grad)
        self.assertEqual(y.grad.data, y_grad)

        grad_sum = 2 * x.grad + y.grad
        x_hv = torch.autograd.grad(
            outputs=[grad_sum], grad_outputs=[torch.ones(2, 2)],
            inputs=[x], create_graph=True, only_inputs=True)
        expected_x_hv = torch.ones(2, 2) * 5
        expected_y_hv = torch.ones(2, 2) * 4

        self.assertEqual(x_hv[0].data, expected_x_hv)
        self.assertEqual(x.grad.data, x_grad)
        self.assertEqual(y.grad.data, y_grad)

        grad_sum = 2 * x.grad + y.grad
        x_hv = torch.autograd.grad(
            outputs=grad_sum, inputs=x,
            grad_outputs=torch.ones(2, 2),
            only_inputs=False)

        self.assertEqual(x_hv[0].data, expected_x_hv)
        self.assertEqual(x.grad.data, x_grad)
        self.assertEqual(y.grad.data, y_grad + expected_y_hv)

    def test_grad_nonleaf(self):
        x_init = Variable(torch.randn(2, 2), requires_grad=True)
        x = x_init
        y = Variable(torch.randn(2, 2), requires_grad=True)
        grad_output = torch.ones(2, 2)

        def fn(x):
            return x ** 2 + y * x + y ** 2

        for i in range(5):
            grad_x, = torch.autograd.grad(
                fn(x), x, grad_outputs=grad_output, create_graph=True)

            grad_x_expected = 2 * x.data + y.data
            self.assertIsNone(y.grad)
            self.assertIsNone(x.grad)
            self.assertEqual(grad_x.data, grad_x_expected)

            x = x + 0.05 * grad_x

        val_init = fn(x_init).data.sum()
        val_final = fn(x).data.sum()
        self.assertGreater(val_final, val_init)

        x.backward(grad_output)
        self.assertIsNotNone(y.grad)
        self.assertIsNotNone(x_init.grad)

    def test_grad_nonleaf_many_outputs(self):
        # This checks an edge case for function callbacks
        # We want to capture two grads of a function, but can only
        # register a single callback.
        x = Variable(torch.randn(4, 2), requires_grad=True)
        a, b = x.chunk(2)

        def hook(*grads):
            hook_called[0] = True
        hook_called = [False]
        x.register_hook(hook)

        go = torch.randn(2, 2)
        grad_a, grad_b = torch.autograd.grad(
            (a + 2 * b), [a, b], grad_outputs=go, create_graph=True)

        self.assertEqual(grad_a.data, go)
        self.assertEqual(grad_b.data, go * 2)
        self.assertFalse(hook_called[0])
        self.assertIsNone(x.grad)

    def test_backward_badcalls(self):
        x = Variable(torch.ones(1))
        with self.assertRaisesRegex(RuntimeError, 'does not require grad'):
            x.backward()

    def test_grad_badcalls(self):
        x = Variable(torch.ones(1))
        y = x ** 2
        with self.assertRaisesRegex(RuntimeError, 'does not require grad'):
            torch.autograd.grad(x, y)
        with self.assertRaisesRegex(RuntimeError, 'does not require grad'):
            torch.autograd.grad(y, x)

        x = Variable(torch.ones(1), requires_grad=True)
        y = x ** 2
        torch.autograd.grad(y, x)  # this should succeed now
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            torch.autograd.grad(x, y)

    def test_grad_unreachable(self):
        x = Variable(torch.ones(1), requires_grad=True)
        y = Variable(torch.ones(1), requires_grad=True)
        # Make sure x and y have grad accumulators allocated
        z = x * 2
        w = y * 2
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            torch.autograd.grad(x * 2, [x, y])

        grad_x, grad_y = torch.autograd.grad(x * 2, [x, y], allow_unused=True)
        self.assertEqual(grad_x, x * 2)
        self.assertIsNone(grad_y)

        # This is slightly different than the case above, because z doesn't even
        # have a grad accumulator allocated.
        z = Variable(torch.ones(1), requires_grad=True)
        grad_x, grad_z = torch.autograd.grad(x * 2, [x, z], allow_unused=True)
        self.assertEqual(grad_x, x * 2)
        self.assertIsNone(grad_z)

    def test_hooks(self):
        x = Variable(torch.ones(5, 5), requires_grad=True)
        y = Variable(torch.ones(5, 5) * 4, requires_grad=True)

        counter = [0]

        def bw_hook(inc, grad):
            self.assertIsInstance(grad, Variable)
            counter[0] += inc

        z = x ** 2 + x * 2 + x * y + y
        x.register_hook(lambda *args: bw_hook(0, *args))
        test = z.register_hook(lambda *args: bw_hook(1, *args))
        z.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(counter[0], 1)

        test2 = z.register_hook(lambda *args: bw_hook(2, *args))
        z.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(counter[0], 4)

        test2.remove()
        z.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(counter[0], 5)

        def bw_hook_modify(grad):
            return grad.mul(2)

        test.remove()
        z.register_hook(bw_hook_modify)
        y.grad.data.zero_()
        z.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(y.grad.data, (x.data + 1) * 2)

        y.register_hook(bw_hook_modify)
        y.grad.data.zero_()
        z.backward(torch.ones(5, 5))
        self.assertEqual(y.grad.data, (x.data + 1) * 4)

    def test_hooks_cpp(self):
        # Tests hooks for autograd function implemented in C++
        bn = torch.nn.BatchNorm1d(5, affine=False)
        bn.eval()

        counter = [0]

        def bw_hook(grad):
            counter[0] += 1
            return grad * 2

        x = Variable(torch.ones(5, 5), requires_grad=True)
        z = bn(x)
        z.register_hook(bw_hook)
        z.sum().backward()

        self.assertEqual(counter[0], 1, 'bw_hook not called')
        self.assertEqual(x.grad.data, torch.ones(5, 5) * 2)

    @unittest.skipIf(sys.version_info[0] == 2, "Python 2 doesn't collect cycles involving __del__")
    def test_hooks_cycle(self):
        import gc
        counter = [0]

        class GradHook(object):
            def __init__(self, var):
                self.var = var

            def __del__(self):
                counter[0] += 1

            def __call__(self, *args):
                pass

        def run_test():
            x = Variable(torch.ones(5, 5), requires_grad=True)
            y = x * 2
            x.register_hook(GradHook(x))
            y.register_hook(GradHook(y))
            y._backward_hooks[1] = GradHook(y)

        run_test()
        gc.collect()
        self.assertEqual(counter[0], 3)

    def test_hook_none(self):
        # WARNING: this is a test for autograd internals.
        # You should never have to use such things in your code.
        class NoneGradientFunction(Function):

            def forward(self, x, y):
                assert self.needs_input_grad[0]
                assert not self.needs_input_grad[1]
                return x, y

            def backward(self, grad_x, grad_y):
                return grad_x, None

        fn = NoneGradientFunction()
        was_called = [False]

        def hook(grad_input, grad_output):
            self.assertIsInstance(grad_input, tuple)
            self.assertIsInstance(grad_output, tuple)
            self.assertIsNotNone(grad_input[0])
            self.assertIsNone(grad_input[1])
            self.assertIsNotNone(grad_output[0])
            self.assertIsNotNone(grad_output[1])
            was_called[0] = True
        fn.register_hook(hook)

        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5))
        sum(fn(x, y)).sum().backward()
        self.assertTrue(was_called[0])

    def test_retain_grad(self):
        input = Variable(torch.rand(1, 3), requires_grad=True)
        h1 = input * 3
        out = (h1 * h1).sum()

        # It should be possible to call retain_grad() multiple times
        h1.retain_grad()
        h1.retain_grad()

        # Gradient should be accumulated
        out.backward(retain_graph=True)
        self.assertEqual(h1.data * 2, h1.grad.data)
        out.backward(retain_graph=True)
        self.assertEqual(h1.data * 4, h1.grad.data)

        input.grad.data.zero_()
        # It should be a no-op for leaves
        input.retain_grad()
        input.retain_grad()
        out.backward()
        self.assertEqual(input.data * 18, input.grad.data)

    def test_retain_grad_cycle(self):
        import gc
        import weakref
        counter = [0]
        refs = [None]

        x = Variable(torch.ones(5, 5), requires_grad=True)

        def run_test():
            y = x * 2
            y.retain_grad()

            def inc(*args):
                counter[0] += 1
            refs[0] = weakref.ref(y, inc)
            return y / 2

        z = run_test()
        gc.collect()
        self.assertIsNone(refs[0]())
        self.assertEqual(counter[0], 1)
        z.sum().backward()

    def test_backward(self):
        v_t = torch.randn(5, 5)
        x_t = torch.randn(5, 5)
        y_t = torch.rand(5, 5) + 0.1
        z_t = torch.randn(5, 5)
        grad_output = torch.randn(5, 5)
        v = Variable(v_t, requires_grad=True)
        x = Variable(x_t, requires_grad=True)
        y = Variable(y_t, requires_grad=True)
        z = Variable(z_t, requires_grad=True)

        v.backward(grad_output)
        self.assertEqual(v.grad.data, grad_output)

        a = x + (y * z) + 4 * z ** 2 * x / y
        a.backward(grad_output)
        x_grad = 4 * z_t.pow(2) / y_t + 1
        y_grad = z_t - 4 * x_t * z_t.pow(2) / y_t.pow(2)
        z_grad = 8 * x_t * z_t / y_t + y_t
        self.assertEqual(x.grad.data, x_grad * grad_output)
        self.assertEqual(y.grad.data, y_grad * grad_output)
        self.assertEqual(z.grad.data, z_grad * grad_output)

    def test_sparse_backward(self):
        class FixedGradientFunction(Function):

            def __init__(self, grad):
                self.grad = grad

            def forward(self, x):
                return x

            def backward(self, grad_x):
                return self.grad

        size = torch.Size([6, 3, 2])
        i1 = torch.LongTensor([
            [0, 3, 4],
            [0, 2, 2],
        ])
        v1 = torch.DoubleTensor([[1, 2], [4, 5], [7, 8]])
        sparse_grad1 = torch.sparse.DoubleTensor(i1, v1, size)
        i2 = torch.LongTensor([
            [0, 1, 3, 4],
            [0, 1, 2, 2],
        ])
        v2 = torch.DoubleTensor([[1, 2], [4, 3], [4, 5], [7, 8]])
        sparse_grad2 = torch.sparse.DoubleTensor(i2, v2, size)
        dense_grad = torch.rand(size).double()
        sparse_fn1 = FixedGradientFunction(sparse_grad1)
        sparse_fn2 = FixedGradientFunction(sparse_grad2)
        dense_fn = FixedGradientFunction(dense_grad)

        # sparse first
        x = Variable(torch.randn(5, 5), requires_grad=True)
        (sparse_fn1(x) + dense_fn(x) + sparse_fn2(x)).sum().backward()
        self.assertEqual(x.grad.data, dense_grad + sparse_grad1 + sparse_grad2)
        # dense first
        x = Variable(torch.randn(5, 5), requires_grad=True)
        (dense_fn(x) + sparse_fn1(x) + sparse_fn2(x)).sum().backward()
        self.assertEqual(x.grad.data, dense_grad + sparse_grad1 + sparse_grad2)
        # sparse only
        x = Variable(torch.randn(5, 5), requires_grad=True)
        (sparse_fn1(x) + sparse_fn2(x)).sum().backward()
        self.assertEqual(x.grad.data, sparse_grad1 + sparse_grad2)

    def test_multi_backward(self):
        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=True)

        q = Variable(torch.randn(5, 5), requires_grad=True)

        a = Variable(torch.randn(5, 5), requires_grad=True)
        b = Variable(torch.randn(5, 5), requires_grad=True)

        q2 = q * 2
        z = x + y + q2
        c = a * b + q2
        grad_z = torch.randn(5, 5)
        grad_c = torch.randn(5, 5)
        torch.autograd.backward([z, c], [grad_z, grad_c])

        self.assertEqual(x.grad.data, grad_z)
        self.assertEqual(y.grad.data, grad_z)
        self.assertEqual(a.grad.data, grad_c * b.data)
        self.assertEqual(b.grad.data, grad_c * a.data)
        self.assertEqual(q.grad.data, (grad_c + grad_z) * 2)

    def test_multi_backward_no_grad(self):
        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=False)

        z = x + y
        q = y * 2

        # NB: we currently raise an exception if any arguments to backwards
        # have requires_grad=False and don't have a grad_fn. We may want to
        # relax that check to a warning.
        def call_backwards():
            torch.autograd.backward([z, q], [torch.ones(5, 5), torch.ones(5, 5)])
        self.assertRaises(RuntimeError, call_backwards)

    def test_dependent_backward(self):
        x = Variable(torch.randn(10), requires_grad=True)
        y = x ** 2
        z = y ** 3

        go_y = torch.randn(10)
        go_z = torch.randn(10)
        torch.autograd.backward([y, z], [go_y, go_z])

        xd = x.data
        self.assertEqual(x.grad.data, 2 * xd * go_y + 6 * xd.pow(5) * go_z)

    def test_save_output_nr(self):
        x = Variable(torch.randn(10), requires_grad=True)

        class MultiOutputFn(Function):
            @staticmethod
            def forward(ctx, x):
                return x[:5], x[5:]

            @staticmethod
            def backward(ctx, *grad):
                return torch.cat(grad)

        a, b = MultiOutputFn.apply(x)
        self.assertEqual(b.output_nr, 1)

        class TestFn(Function):
            @staticmethod
            def forward(ctx, b):
                ctx.save_for_backward(b)
                return b * 2

            @staticmethod
            def backward(ctx, grad_b):
                b, = ctx.saved_variables
                self.assertEqual(b.output_nr, 1)

        TestFn.apply(b).sum().backward()

    def test_volatile(self):
        x = Variable(torch.ones(5, 5), requires_grad=True)
        y = Variable(torch.ones(5, 5) * 4, volatile=True)

        z = x ** 2
        self.assertFalse(z.volatile)
        self.assertTrue(z.requires_grad)
        self.assertIsNotNone(z.grad_fn)
        z.backward(torch.ones(5, 5))
        self.assertEqual(x.grad.data, torch.ones(5, 5) * 2)

        w = z + y
        self.assertTrue(w.volatile)
        self.assertFalse(w.requires_grad)
        self.assertRaises(RuntimeError, lambda: w.backward(torch.ones(5, 5)))
        self.assertIsNone(w.grad_fn)

    def test_indexing(self):
        x = torch.arange(1, 17).view(4, 4)
        y = Variable(x, requires_grad=True)

        def compare(x, y, idx, indexed_tensor, indexed_var):
            indexed_var_t = indexed_var.data
            if not torch.is_tensor(indexed_tensor):
                indexed_var_t = indexed_var_t[0]
            self.assertEqual(indexed_tensor, indexed_var_t)

            indexed_var.sum().backward()
            expected_grad = torch.Tensor(x.size()).fill_(0)
            expected_grad[idx] = 1
            self.assertEqual(y.grad.data, expected_grad)

        def check_index(x, y, idx):
            if y.grad is not None:
                y.grad.data.zero_()
            indexed_tensor = x[idx]
            indexed_var = y[idx]
            compare(x, y, idx, indexed_tensor, indexed_var)

        check_index(x, y, 1)
        check_index(x, y, (1, 1))
        check_index(x, y, slice(1, None))
        check_index(x, y, slice(None, 2))
        check_index(x, y, (slice(None, 2), 2))
        check_index(x, y, (slice(1, 2), 2))
        check_index(x, y, (1, slice(2, None)))
        check_index(x, y, (slice(None, None), slice(2, None)))
        check_index(x, y, torch.LongTensor([0, 2]))
        check_index(x, y, torch.rand(4, 4).bernoulli().byte())
        check_index(x, y, (Ellipsis, slice(2, None)))
        check_index(x, y, ([0], [0]))
        check_index(x, y, ([1, 2, 3], [0]))
        check_index(x, y, ([1, 2], [2, 1]))
        check_index(x, y, ([[1, 2], [3, 0]], [[0, 1], [2, 3]]))
        check_index(x, y, ([slice(None), [2, 3]]))
        check_index(x, y, ([[2, 3], slice(None)]))

        # advanced indexing, with less dim, or ellipsis
        check_index(x, y, ([0]))
        check_index(x, y, ([0], ))

        x = torch.arange(1, 49).view(4, 3, 4)
        y = Variable(x, requires_grad=True)

        check_index(x, y, (slice(None), [0], [0]))
        check_index(x, y, ([0], [0], slice(None)))
        check_index(x, y, (slice(None), [0, 1, 2], [0]))
        check_index(x, y, ([0, 1, 2], [0], slice(None)))
        check_index(x, y, (slice(None), [1, 2], [2, 1]))
        check_index(x, y, ([1, 2], [2, 1], slice(None)))
        check_index(x, y, (slice(None), [[1, 2], [2, 0]], [[0, 1], [2, 3]]))
        check_index(x, y, ([[1, 2], [3, 0]], [[0, 1], [2, 2]], slice(None)))
        check_index(x, y, (slice(None), slice(None), [2, 1]))
        check_index(x, y, (slice(None), [2, 1], slice(None)))
        check_index(x, y, ([2, 1], slice(None), slice(None)))

        # advanced indexing, with less dim, or ellipsis
        check_index(x, y, ([0], ))
        check_index(x, y, ([0], slice(None)))
        check_index(x, y, ([0], Ellipsis))
        check_index(x, y, ([1, 2], [0, 1]))
        check_index(x, y, ([1, 2], [0, 1], Ellipsis))
        check_index(x, y, (Ellipsis, [1, 2], [0, 1]))

        # advanced indexing, with a tensor wrapped in a variable
        z = torch.LongTensor([0, 1])
        zv = Variable(z, requires_grad=False)
        seq = [z, Ellipsis]
        seqv = [zv, Ellipsis]

        if y.grad is not None:
            y.grad.data.zero_()
        indexed_tensor = x[seq]
        indexed_var = y[seqv]
        compare(x, y, seq, indexed_tensor, indexed_var)

    def test_indexing_duplicates(self):
        x = torch.arange(1, 17).view(4, 4)
        y = Variable(x, requires_grad=True)

        idx = torch.LongTensor([1, 1, 3, 2, 1, 2])
        y[idx].sum().backward()
        expected_grad = torch.zeros(4, 4)
        for i in idx:
            expected_grad[i] += 1
        self.assertEqual(y.grad.data, expected_grad)

        # with advanced indexing
        x = torch.arange(1, 17).view(4, 4)
        y = Variable(x, requires_grad=True)

        idx = [[1, 1, 3, 2, 1, 2], [0]]
        y[idx].sum().backward()
        expected_grad = torch.zeros(4, 4)
        for i in idx[0]:
            for j in idx[1]:
                expected_grad[i][j] += 1

        self.assertEqual(y.grad.data, expected_grad)

        x = torch.arange(1, 17).view(4, 4)
        y = Variable(x, requires_grad=True)
        idx = [[[1, 2], [0, 0]], [[0, 1], [1, 1]]]
        y[idx].sum().backward()
        expected_grad = torch.Tensor([[0, 2, 0, 0],
                                      [1, 0, 0, 0],
                                      [0, 1, 0, 0],
                                      [0, 0, 0, 0]])
        self.assertEqual(y.grad.data, expected_grad)

        x = torch.arange(1, 65).view(4, 4, 4)
        y = Variable(x, requires_grad=True)

        idx = [[1, 1, 1], slice(None), slice(None)]
        y[idx].sum().backward()
        expected_grad = torch.Tensor(4, 4, 4).zero_()
        expected_grad[1].fill_(3)
        self.assertEqual(y.grad.data, expected_grad)

    def test_requires_grad(self):
        x = Variable(torch.randn(5, 5))
        y = Variable(torch.randn(5, 5))
        z = Variable(torch.randn(5, 5), requires_grad=True)
        a = x + y
        self.assertFalse(a.requires_grad)
        b = a + z
        self.assertTrue(b.requires_grad)

        def error():
            raise RuntimeError
        # Make sure backward isn't called on these
        a._backward_hooks = OrderedDict()
        x._backward_hooks = OrderedDict()
        y._backward_hooks = OrderedDict()
        a._backward_hooks['test'] = error
        x._backward_hooks['test'] = error
        y._backward_hooks['test'] = error
        b.backward(torch.ones(5, 5))

    def test_requires_grad_inplace(self):
        a = Variable(torch.randn(5, 5))
        b = Variable(torch.randn(5, 5), requires_grad=True)
        a += b
        self.assertTrue(a.requires_grad)

        # non-leaf Variable
        a = Variable(torch.randn(5, 5)) + 0
        b = Variable(torch.randn(5, 5), requires_grad=True)
        a += b
        self.assertTrue(a.requires_grad)

    def test_grad_assignment(self):
        x = Variable(torch.randn(5, 5))
        a = Variable(torch.randn(2, 2))  # size mismatch
        b = Variable(torch.randn(5, 5).long())  # type mismatch

        with self.assertRaises(RuntimeError):
            x.grad = Variable(torch.randn(2, 2))
        with self.assertRaises(RuntimeError):
            x.grad = Variable(torch.randn(5, 5).long())
        with self.assertRaises(RuntimeError):
            x.grad = x

        if not torch.cuda.is_available():
            raise unittest.SkipTest("CUDA not available")
        with self.assertRaises(RuntimeError):
            x.grad = Variable(torch.randn(5, 5).cuda())

        if torch.cuda.device_count() < 2:
            raise unittest.SkipTest("At least 2 CUDA devices needed")
        x = Variable(torch.randn(5, 5).cuda(0))
        with self.assertRaises(RuntimeError):
            x.grad = Variable(torch.randn(5, 5).cuda(1))

    def test_duplicate_backward_root(self):
        a = Variable(torch.randn(5, 5), requires_grad=True)
        b = Variable(torch.randn(5, 5), requires_grad=True)

        x = a * b
        grad_output = x.data.clone().normal_()
        torch.autograd.backward([x, x], [grad_output, grad_output])

        self.assertEqual(a.grad.data, b.data * grad_output * 2)
        self.assertEqual(b.grad.data, a.data * grad_output * 2)

    def test_backward_no_grad(self):
        a = Variable(torch.randn(5, 5), requires_grad=True)
        b = a + 2
        with self.assertRaises(RuntimeError):
            torch.autograd.backward([b], [None])

    def test_next_functions(self):
        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=True)

        a = x + y
        self.assertIsNotNone(a.grad_fn)
        next_functions = a.grad_fn.next_functions
        self.assertEqual(len(next_functions), 2)
        self.assertIsInstance(next_functions[0][0], torch._C._functions.AccumulateGrad)
        self.assertEqual(next_functions[0][1], 0)
        self.assertIsInstance(next_functions[1][0], torch._C._functions.AccumulateGrad)
        self.assertEqual(next_functions[1][1], 0)

        b = a + 5
        next_functions = b.grad_fn.next_functions
        self.assertEqual(len(next_functions), 1)
        self.assertIs(next_functions[0][0], a.grad_fn)

    def test_inplace(self):
        x = Variable(torch.ones(5, 5), requires_grad=True)
        y = Variable(torch.ones(5, 5) * 4, requires_grad=True)

        z = x * y
        q = z + y
        w = z * y
        z.add_(2)
        # Add doesn't need it's inputs to do backward, so it shouldn't raise
        q.backward(torch.ones(5, 5), retain_graph=True)
        # Mul saves both inputs in forward, so it should raise
        self.assertRaises(RuntimeError, lambda: w.backward(torch.ones(5, 5)))

        z = x * y
        q = z * y
        r = z + y
        w = z.add_(y)
        # w is a the last expression, so this should succeed
        w.backward(torch.ones(5, 5), retain_graph=True)
        # r doesn't use the modified value in backward, so it should succeed
        r.backward(torch.ones(5, 5), retain_graph=True)
        # q uses dirty z, so it should raise
        self.assertRaises(RuntimeError, lambda: q.backward(torch.ones(5, 5)))

        x.grad.data.zero_()
        m = x / 2
        z = m + y / 8
        q = z * y
        r = z + y
        prev_version = z._version
        w = z.exp_()
        self.assertNotEqual(z._version, prev_version)
        r.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(x.grad.data, torch.ones(5, 5) / 2)
        w.backward(torch.ones(5, 5), retain_graph=True)
        self.assertEqual(x.grad.data, torch.Tensor(5, 5).fill_((1 + math.e) / 2))
        self.assertRaises(RuntimeError, lambda: q.backward(torch.ones(5, 5)))

        leaf = Variable(torch.ones(5, 5), requires_grad=True)
        x = leaf.clone()
        x.add_(10)
        self.assertEqual(x.data, torch.ones(5, 5) * 11)
        # x should be still usable
        y = x + 2
        y.backward(torch.ones(5, 5))
        self.assertEqual(leaf.grad.data, torch.ones(5, 5))
        z = x * y
        x.add_(2)
        self.assertRaises(RuntimeError, lambda: z.backward(torch.ones(5, 5)))

    def test_mark_non_differentiable(self):
        class MyFunction(Function):
            @staticmethod
            def forward(ctx, input):
                output = input > 0
                ctx.mark_non_differentiable(output)
                return output

            @staticmethod
            def backward(ctx, grad_output):
                return (grad_output * 0).type(torch.DoubleTensor)

        x = Variable(torch.randn(5, 5), requires_grad=True)
        mask = MyFunction.apply(x)
        self.assertFalse(mask.requires_grad)
        y = x.masked_fill(mask, 0)
        y.sum().backward()

    def test_resize(self):
        x = Variable(torch.ones(2, 3))
        self.assertTrue(x.resize(3, 2).size() == (3, 2))

    def test_shared_storage(self):
        x = Variable(torch.ones(5, 5))
        y = x.t()
        z = x[1]
        self.assertRaises(RuntimeError, lambda: x.add_(2))
        self.assertRaises(RuntimeError, lambda: y.add_(2))
        self.assertRaises(RuntimeError, lambda: z.add_(2))

    def _test_setitem(self, size, index):
        x = Variable(torch.ones(*size), requires_grad=True)
        y = x + 2
        y_version = y._version
        y[index] = 2
        self.assertNotEqual(y._version, y_version)
        y.backward(torch.ones(*size))
        expected_grad = torch.ones(*size)
        if isinstance(index, Variable):
            index = index.data
        expected_grad[index] = 0
        self.assertEqual(x.grad.data, expected_grad)

    def _test_setitem_tensor(self, size, index):
        x = Variable(torch.ones(*size), requires_grad=True)
        y = x + 2
        y_version = y._version
        value = Variable(torch.Tensor(x[index].size()).fill_(7), requires_grad=True)
        y[index] = value
        self.assertNotEqual(y._version, y_version)
        y.backward(torch.ones(*size))
        expected_grad_input = torch.ones(*size)

        # remove all variables when indexing a Tensor for comparison,
        # whether a top-level Variable or in a sequence
        if isinstance(index, Variable):
            index = index.data
        elif isinstance(index, list):
            novars = []
            for i in index:
                if isinstance(i, Variable):
                    novars.append(i.data)
                else:
                    novars.append(i)
            index = novars

        expected_grad_input[index] = 0
        self.assertEqual(x.grad.data, expected_grad_input)
        self.assertEqual(value.grad.data, torch.ones(value.size()))

        # case when x is not same shape as y[1]
        x = Variable(torch.randn(1, 2), requires_grad=True)
        y = Variable(torch.zeros(10, 2))
        y[1] = x
        y.backward(torch.randn(10, 2))
        self.assertEqual(x.size(), x.grad.size())

    def test_setitem(self):
        self._test_setitem((5, 5), 1)
        self._test_setitem((5,), 1)
        self._test_setitem((1,), 0)
        self._test_setitem((10,), [[0, 4, 2]])
        self._test_setitem((5, 5), [[0, 4], [2, 2]])
        self._test_setitem((5, 5, 5), [slice(None), slice(None), [1, 3]])
        self._test_setitem((5, 5, 5), [slice(None), [1, 3], slice(None)])
        self._test_setitem((5, 5, 5), [[1, 3], slice(None), slice(None)])
        self._test_setitem((5, 5, 5), [slice(None), [2, 4], [1, 3]])
        self._test_setitem((5, 5, 5), [[1, 3], [2, 4], slice(None)])
        self._test_setitem_tensor((5, 5), 3)
        self._test_setitem_tensor((5, 5), [[0, 1], [1, 0]])
        self._test_setitem_tensor((5,), 3)
        self._test_setitem_tensor((5,), [[0, 1, 2, 3]])
        self._test_setitem_tensor((5, 5, 5), [slice(None), slice(None), [1, 3]])
        self._test_setitem_tensor((5, 5, 5), [slice(None), [1, 3], slice(None)])
        self._test_setitem_tensor((5, 5, 5), [[1, 3], slice(None), slice(None)])
        self._test_setitem_tensor((5, 5, 5), [slice(None), [2, 4], [1, 3]])
        self._test_setitem_tensor((5, 5, 5), [[1, 3], [2, 4], slice(None)])
        self._test_setitem_tensor((5, 5, 5), [Variable(torch.LongTensor([1,
                                              3]), requires_grad=False), [2, 4], slice(None)])

    def test_setitem_mask(self):
        mask = torch.ByteTensor(5, 5).bernoulli_()
        self._test_setitem((5, 5), Variable(mask))
        self._test_setitem((5,), Variable(mask[0]))
        self._test_setitem((1,), Variable(mask[0, 0:1]))
        self._test_setitem_tensor((5, 5), Variable(mask))
        self._test_setitem_tensor((5,), Variable(mask[0]))

    def test_stack(self):
        x = Variable(torch.randn(10, 10), requires_grad=True)
        y = Variable(torch.randn(10, 10), requires_grad=True)
        z = Variable(torch.randn(10, 10), requires_grad=True)
        stacked = torch.stack([x, y, z], 0)
        grad = torch.randn(3, 10, 10)
        stacked.backward(grad)
        self.assertEqual(x.grad.data, grad[0])
        self.assertEqual(y.grad.data, grad[1])
        self.assertEqual(z.grad.data, grad[2])

    def test_unused_output(self):
        x = Variable(torch.randn(10, 10), requires_grad=True)
        outputs = x.chunk(5)
        o = outputs[2]
        o = o * 4 + 2
        o.sum().backward()
        expected_grad = torch.zeros(10, 10)
        expected_grad[4:6] = 4
        self.assertEqual(x.grad.data, expected_grad)

        x.grad.data.zero_()
        grad_output = torch.randn(2, 10)
        outputs = x.chunk(5)
        outputs[0].backward(grad_output)
        expected_grad = torch.zeros(10, 10)
        expected_grad[:2] = grad_output
        self.assertEqual(x.grad.data, expected_grad)

    def test_gc_in_destructor(self):
        """
        Previously, if a Function destructor triggered a garbage collection,
        the Variable's tp_dealloc handler would get called twice leading to a
        segfault.
        """
        class CollectOnDelete(Function):

            def __del__(self):
                gc.collect()

        for i in range(10):
            Variable(torch.randn(10, 10), _grad_fn=CollectOnDelete())

    @unittest.skipIf(torch.cuda.device_count() < 2, "no multi-GPU")
    def test_unused_output_gpu(self):
        from torch.nn.parallel._functions import Broadcast
        x = Variable(torch.randn(5, 5).float().cuda(), requires_grad=True)
        outputs = Broadcast.apply(list(range(torch.cuda.device_count())), x)
        y = outputs[-1] * 2
        y.sum().backward()
        self.assertEqual(x.grad.data, torch.ones(5, 5) * 2)

    @unittest.skipIf(torch.cuda.device_count() < 2, "no multi-GPU")
    def test_backward_device(self):
        # check that current device matches the variable's device
        device = [None]

        class Identity(torch.autograd.Function):
            @staticmethod
            def forward(ctx, x):
                return x.clone()

            @staticmethod
            def backward(ctx, grad_output):
                device[0] = torch.cuda.current_device()
                return grad_output.clone()

        v = Variable(torch.randn(1).cuda(1), requires_grad=True)
        Identity.apply(v).backward()
        self.assertEqual(device[0], 1)

    def test_detach(self):
        x = Variable(torch.randn(10, 10), requires_grad=True)
        y = x + 2
        y = y.detach()
        z = y * 4 + 2
        self.assertFalse(y.requires_grad)
        self.assertFalse(z.requires_grad)

        x = Variable(torch.randn(10, 10), requires_grad=True)
        y = x * 2
        y = y.detach()
        self.assertFalse(y.requires_grad)
        self.assertIsNone(y.grad_fn)
        z = x + y
        z.sum().backward()
        # This is an incorrect gradient, but we assume that's what the user
        # wanted. detach() is an advanced option.
        self.assertEqual(x.grad.data, torch.ones(10, 10))

        # detach() should preserve volatile flag
        x = Variable(torch.randn(10, 10), volatile=True)
        y = x * 2
        y = y.detach()
        self.assertTrue(y.volatile)

        # in-place detach
        x = Variable(torch.randn(10, 10), requires_grad=True)
        y = Variable(torch.randn(10, 10), requires_grad=True)
        a = x * 2
        (y + a).sum().backward(retain_graph=True)
        a.detach_()
        self.assertFalse(a.requires_grad)
        (y + a).sum().backward()  # this won't backprop to x
        self.assertEqual(x.grad.data, torch.ones(10, 10) * 2)
        self.assertEqual(y.grad.data, torch.ones(10, 10) * 2)

    def test_type_conversions(self):
        x = Variable(torch.randn(5, 5))
        self.assertIs(type(x.float().data), torch.FloatTensor)
        self.assertIs(type(x.int().data), torch.IntTensor)
        if torch.cuda.is_available():
            self.assertIs(type(x.float().cuda().data), torch.cuda.FloatTensor)
            self.assertIs(type(x.int().cuda().data), torch.cuda.IntTensor)
            self.assertIs(type(x.int().cuda().cpu().data), torch.IntTensor)
            if torch.cuda.device_count() >= 2:
                x2 = x.float().cuda(1)
                self.assertIs(type(x2.data), torch.cuda.FloatTensor)
                self.assertIs(x2.get_device(), 1)
                x2 = x.float().cuda()
                self.assertIs(type(x2.data), torch.cuda.FloatTensor)
                self.assertIs(x2.get_device(), 0)
                x2 = x2.cuda(1)
                self.assertIs(type(x2.data), torch.cuda.FloatTensor)
                self.assertIs(x2.get_device(), 1)
                y = Variable(torch.randn(5).cuda(1), requires_grad=True)
                y.cpu().sum().backward()
                self.assertIs(y.grad.get_device(), 1)
                self.assertIs(y.long().data.get_device(), 1)

        for t in [torch.DoubleTensor, torch.FloatTensor, torch.IntTensor, torch.ByteTensor]:
            for var in (True, False):
                y = torch.randn(5, 5).type(t)
                if var:
                    y = Variable(y)
                self.assertIs(type(x.type_as(y).data), t)

    def test_isolated_node(self):
        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=True)

        a = x + y
        b = torch.max(a, 1, True)[1].repeat(1, 5).double()
        o = (b + a).sum()
        o.backward()

    def test_shape(self):
        x = Variable(torch.randn(3, 4))
        self.assertEqual(2, len(x.shape))
        self.assertEqual(x.shape[0], 3)
        self.assertEqual(x.shape[1], 4)

    def test_return_leaf(self):
        class Identity(Function):

            def forward(self, a, b):
                return a, a + b

            def backward(self, grad_a, grad_b):
                return grad_a + grad_b, grad_b

        class Inplace(InplaceFunction):

            def forward(self, a, b):
                self.mark_dirty(a)
                return a.add_(b), b + 2

            def backward(self, grad_a, grad_b):
                return grad_a, grad_a + grad_b

        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = Variable(torch.randn(5, 5), requires_grad=True)

        q, p = Identity()(x, y)
        # Make sure hooks only receive grad from usage of q, not x.
        q.register_hook(
            lambda grad: self.assertEqual(grad.data, torch.ones(5, 5)))
        (q + p + x).sum().backward()
        self.assertEqual(x.grad.data, torch.ones(5, 5) * 3)
        self.assertEqual(y.grad.data, torch.ones(5, 5))
        del q, p  # these need to be freed, or next part will raise an error

    def test_return_leaf_inplace(self):
        class Inplace(InplaceFunction):

            def forward(self, a, b):
                self.mark_dirty(a)
                return a.add_(b), b + 2

            def backward(self, grad_a, grad_b):
                return grad_a, grad_a + grad_b

        x = Variable(torch.randn(5, 5))
        y = Variable(torch.randn(5, 5), requires_grad=True)

        fn = Inplace(True)
        q, p = fn(x, y)
        self.assertIs(q, x)
        self.assertIs(q.grad_fn, fn)
        self.assertTrue(q.requires_grad)
        q.sum().backward()
        self.assertEqual(y.grad.data, torch.ones(5, 5))

    def test_leaf_assignment(self):
        x = Variable(torch.randn(5, 5))
        y = Variable(torch.randn(5), requires_grad=True)
        z = Variable(torch.randn(5), requires_grad=True)

        x[0] = y
        x[1] = 2 * z
        self.assertTrue(x.requires_grad)
        self.assertIsNot(x.grad_fn, None)
        x.sum().backward()
        self.assertEqual(y.grad.data, torch.ones(5))
        self.assertEqual(z.grad.data, torch.ones(5) * 2)

    def test_volatile_assignment(self):
        x = Variable(torch.randn(5, 5))
        y = Variable(torch.randn(5), volatile=True)

        x[0] = y
        self.assertTrue(x.volatile)

    def test_backward_copy(self):
        # This tests checks backward engine for a very subtle bug that appreared
        # in one of the initial versions of autograd. Gradients tensors were
        # simply stored in lists while the function waited for all its gradients
        # to be computed. However, sometimes an output was used multiple times,
        # so the gradients needed to be summed. Engine used to keep a need_copy
        # set of tensors that will need a clone upon next addition and removed
        # them from the set as soon as the clone was performed. However, this
        # could lead to incorrect results if the same gradient tensor was
        # buffered in three places in the graph:
        # 1. When accumulating gradients in one of these places it was cloned
        #    and removed from need_copy set.
        # 2. When accumulating in second place, it wasn't in the need_copy set,
        #    so the gradients were simply accumulated in-place (which already
        #    modified the grad in 3rd place)
        # 3. When accumulating in the third place, it wasn't in the need_copy set
        #    as well, so the incoming gradient was summed in-place, yielding
        #    incorrect results in all functions, except the first one.
        x = Variable(torch.ones(5, 5), requires_grad=True)
        y = Variable(torch.ones(5, 5), requires_grad=True)
        # Simulate that we're in the middle of the graph
        a = x + 2
        b = y + 2
        c = x + 2
        # This op will just return grad_output two times in backward
        add1 = a + b
        add2 = add1 + c
        # Simulate a long branch, so grad_output will get buffered.
        for i in range(4):
            a = a * 2
            b = b * 2
            c = c * 2
        branch = a + b + c
        out = add2 + branch
        # expected gradients are:
        # for x: 34 (16 from final a, 16 from final c, 2 from add2)
        # for y: 17 (16 from final b, 1 from add2)
        grad_output = torch.ones(5, 5)
        out.backward(grad_output)
        self.assertEqual(x.grad.data, torch.ones(5, 5) * 34)
        self.assertEqual(y.grad.data, torch.ones(5, 5) * 17)

    def test_functional_blas(self):
        def compare(fn, *args):
            unpacked_args = tuple(arg.data if isinstance(arg, Variable) else arg
                                  for arg in args)
            unpacked_result = fn(*unpacked_args)
            packed_result = fn(*args).data
            # if non-Variable torch function returns a scalar, compare to scalar
            if not torch.is_tensor(unpacked_result):
                assert packed_result.dim() == 1
                assert packed_result.nelement() == 1
                packed_result = packed_result[0]
            self.assertEqual(packed_result, unpacked_result)

        def test_blas_add(fn, x, y, z):
            # Checks all signatures
            compare(fn, x, y, z)
            compare(fn, 0.5, x, y, z)
            compare(fn, 0.5, x, 0.25, y, z)

        def test_blas(fn, x, y):
            compare(fn, x, y)

        test_blas(torch.mm, Variable(torch.randn(2, 10)),
                  Variable(torch.randn(10, 4)))
        test_blas_add(torch.addmm, Variable(torch.randn(2, 4)),
                      Variable(torch.randn(2, 10)), Variable(torch.randn(10, 4)))
        test_blas(torch.bmm, Variable(torch.randn(4, 2, 10)),
                  Variable(torch.randn(4, 10, 4)))
        test_blas_add(torch.addbmm, Variable(torch.randn(2, 4)),
                      Variable(torch.randn(4, 2, 10)), Variable(torch.randn(4, 10, 4)))
        test_blas_add(torch.baddbmm, Variable(torch.randn(4, 2, 4)),
                      Variable(torch.randn(4, 2, 10)), Variable(torch.randn(4, 10, 4)))
        test_blas(torch.mv, Variable(torch.randn(2, 10)),
                  Variable(torch.randn(10)))
        test_blas_add(torch.addmv, Variable(torch.randn(2)),
                      Variable(torch.randn(2, 10)), Variable(torch.randn(10)))
        test_blas(torch.ger, Variable(torch.randn(5)),
                  Variable(torch.randn(6)))
        test_blas_add(torch.addr, Variable(torch.randn(5, 6)),
                      Variable(torch.randn(5)), Variable(torch.randn(6)))
        test_blas(torch.matmul, Variable(torch.randn(6)), Variable(torch.randn(6)))
        test_blas(torch.matmul, Variable(torch.randn(10, 4)), Variable(torch.randn(4)))
        test_blas(torch.matmul, Variable(torch.randn(5)), Variable(torch.randn(5, 6)))
        test_blas(torch.matmul, Variable(torch.randn(2, 10)), Variable(torch.randn(10, 4)))
        test_blas(torch.matmul, Variable(torch.randn(5, 2, 10)), Variable(torch.randn(5, 10, 4)))
        test_blas(torch.matmul, Variable(torch.randn(3, 5, 2, 10)), Variable(torch.randn(3, 5, 10, 4)))
        test_blas(torch.matmul, Variable(torch.randn(3, 5, 2, 10)), Variable(torch.randn(10)))
        test_blas(torch.matmul, Variable(torch.randn(10)), Variable(torch.randn(3, 5, 10, 4)))

    def test_save_none_for_backward(self):
        test_case = self

        class MyFn(Function):

            def forward(self, input):
                self.save_for_backward(None, input, None)
                return input * input

            def backward(self, grad_output):
                n1, input, n2 = self.saved_tensors
                test_case.assertIsNone(n1)
                test_case.assertIsNone(n2)
                return 2 * input * grad_output

        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = MyFn()(x)
        y.sum().backward()
        self.assertEqual(x.grad.data, 2 * x.data)

    def test_too_many_grads(self):
        class MyFn(Function):

            def forward(self, input):
                return input

            def backward(self, grad_output):
                return grad_output, None, None

        x = Variable(torch.randn(5, 5), requires_grad=True)
        y = MyFn()(x)
        y.sum().backward()
        self.assertEqual(x.grad.data, x.data.clone().fill_(1))

    def test_pickle(self):
        x = Variable(torch.randn(10, 10), requires_grad=True)
        y = Variable(torch.randn(10, 10), volatile=True)
        z = Variable(torch.randn(10, 10), requires_grad=False)

        def assert_strict_equal(var1, var2):
            self.assertEqual(var1.data, var2.data)
            self.assertEqual(var1.requires_grad, var2.requires_grad)
            self.assertEqual(var1.volatile, var2.volatile)

        serialized = [pickle.dumps([x, y, z], protocol=p) for p in range(3)]
        for dump in serialized:
            xc, yc, zc = pickle.loads(dump)
            assert_strict_equal(xc, x)
            assert_strict_equal(yc, y)
            assert_strict_equal(zc, z)

    def test_dep_nograd(self):
        class F1(Function):

            def forward(self, input):
                out = torch.randn(input.size())
                self.mark_non_differentiable(out)
                return input, out

            def backward(self, grad_output, ignored):
                return grad_output

        class F2(Function):

            def forward(self, input, ignored):
                return input

            def backward(self, grad_output):
                return grad_output, None

        x = Variable(torch.randn(5), requires_grad=True)
        a, b = F1()(x)
        b = b + 1  # separate F1 from F2 by another op
        self.assertTrue(a.requires_grad)
        self.assertFalse(b.requires_grad)
        c = F2()(a, b)
        c.backward(torch.ones(c.size()))
        self.assertEqual(x.grad.data, torch.ones(x.size()))

    def test_reentrant(self):
        y_data = torch.randn(2, 2)

        class Reenter(Function):
            @staticmethod
            def forward(ctx, x_data):
                ctx.x = Variable(x_data, requires_grad=True)
                ctx.y = Variable(y_data, requires_grad=True)
                ctx.output_var = ctx.x * ctx.y
                return ctx.output_var.data

            @staticmethod
            def backward(ctx, grad_output):
                ctx.output_var.sum().backward()
                return ctx.x.grad * grad_output

        x = Variable(torch.randn(2, 2), requires_grad=True)
        out = Reenter.apply(x)
        out.sum().backward()
        self.assertEqual(x.grad.data, y_data)

    def test_cat(self):
        f_args_variable = (Variable(torch.randn(1, S, S), requires_grad=True),
                           Variable(torch.randn(2, S, S), requires_grad=True),
                           Variable(torch.randn(3, S, S), requires_grad=True),
                           0)
        f_args_tensor = deepcopy(unpack_variables(f_args_variable))
        run_functional_checks(self, "test_cat", "cat",
                              lambda a, b, c, dim: torch.cat((a, b, c), dim),
                              True, f_args_variable, f_args_tensor)

    def test_cat_negdim_1(self):
        f_args_variable = (Variable(torch.randn(S, S, 1), requires_grad=True),
                           Variable(torch.randn(S, S, 2), requires_grad=True),
                           Variable(torch.randn(S, S, 3), requires_grad=True),
                           -1)
        f_args_tensor = deepcopy(unpack_variables(f_args_variable))
        run_functional_checks(self, "test_cat_negdim_1", "cat",
                              lambda a, b, c, dim: torch.cat((a, b, c), dim),
                              True, f_args_variable, f_args_tensor)

    def test_cat_negdim_2(self):
        f_args_variable = (Variable(torch.randn(S, 1, S), requires_grad=True),
                           Variable(torch.randn(S, 2, S), requires_grad=True),
                           Variable(torch.randn(S, 3, S), requires_grad=True),
                           -2)
        f_args_tensor = deepcopy(unpack_variables(f_args_variable))
        run_functional_checks(self, "test_cat_negdim_2", "cat",
                              lambda a, b, c, dim: torch.cat((a, b, c), dim),
                              True, f_args_variable, f_args_tensor)

    def test_variable_traverse(self):
        def get_out_and_unrefed_cycle():
            inp = Variable(torch.randn(10), requires_grad=True)
            tmp = inp.view(10, 1)
            out = tmp.view(10)

            # Create a reference cycle that contains an
            # intermediary Variable in the graph
            my_list = []
            my_list.append(tmp)
            my_list.append(my_list)

            return out

        out = get_out_and_unrefed_cycle()
        gc.collect()
        # This will segfault if things have been erroneously released
        out.backward(torch.randn(out.size()))

    def test_norm_subgradient(self):
        def run_test(input_size, norm_deg):
            input = Variable(torch.zeros(*input_size), requires_grad=True)
            out = input.norm(norm_deg)
            out.backward()
            self.assertEqual(input.grad.data.abs().sum(), 0)

        run_test((10,), 2)
        run_test((10, 10), 2)
        run_test((10,), 3)

    def test_profiler(self):
        x = Variable(torch.randn(10, 10))

        with profile() as p:
            y = x * 2 + 4

        last_end = 0
        names = ['MulConstant', 'AddConstant']
        for info, expected_name in zip(p.function_events, names):
            self.assertGreater(info.start, last_end)
            self.assertEqual(info.name, expected_name)
            last_end = info.end


def index_variable(shape, max_indices):
    if not isinstance(shape, tuple):
        shape = (shape,)
    index = torch.rand(*shape).mul_(max_indices).floor_().long()
    return Variable(index, requires_grad=False)


def index_perm_variable(shape, max_indices):
    if not isinstance(shape, tuple):
        shape = (shape,)

    index = torch.randperm(max_indices).narrow(0, 0, reduce(mul, shape)).view(shape)
    return Variable(index, requires_grad=False)


def gather_variable(shape, index_dim, max_indices, duplicate=False):
    assert len(shape) == 2
    assert index_dim < 2
    batch_dim = 1 - index_dim
    index = torch.LongTensor(*shape)
    for i in range(shape[index_dim]):
        index.select(index_dim, i).copy_(
            torch.randperm(max_indices)[:shape[batch_dim]])
    if duplicate:
        index.select(batch_dim, 0).copy_(index.select(batch_dim, 1))
    return Variable(index, requires_grad=False)


def mask_not_all_zeros(shape):
    assert len(shape) > 0
    while True:
        result = torch.randn(shape).gt(0)
        if result.sum() > 0:
            return result


def prod_zeros(dim_size, dim_select):
    assert len(dim_select) == 2
    result = torch.randn(dim_size, dim_size, dim_size)
    result.narrow(dim_select[0], 0, 1).narrow(dim_select[1], 1, 1).zero_()
    result.narrow(dim_select[0], 2, 1).narrow(dim_select[1], 3, 1).zero_()
    result.narrow(dim_select[0], 4, 1).narrow(dim_select[1], 3, 1).zero_()
    return Variable(result, requires_grad=True)


def prod_single_zero(dim_size):
    result = torch.randn(dim_size, dim_size)
    result[0, 1] = 0
    return Variable(result, requires_grad=True)


def _make_cov(S):
    L = torch.tril(torch.rand(S, S))
    return torch.mm(L, L.t())


class dont_convert(tuple):
    pass


L = 20
M = 10
S = 5


# (name, size, args...)
method_tests = [
    ('add', (S, S, S), ((S, S, S),)),
    ('add', (S, S, S), ((S, S),), 'broadcast_rhs'),
    ('add', (S, S), ((S, S, S),), 'broadcast_lhs'),
    ('add', (S, 1, S), ((M, S),), 'broadcast_all'),
    ('add', (S, S, S), (3.14,), 'constant'),
    ('__radd__', (S, S, S), (3.14,), 'constant'),
    ('sub', (S, S, S), ((S, S, S),)),
    ('sub', (S, S, S), ((S, S),), 'broadcast_rhs'),
    ('sub', (S, S), ((S, S, S),), 'broadcast_lhs'),
    ('sub', (S, 1, S), ((M, S),), 'broadcast_all'),
    ('sub', (S, S, S), (3.14,), 'constant'),
    ('__rsub__', (S, S, S), (3.14,), 'constant'),
    ('mul', (S, S, S), ((S, S, S),)),
    ('mul', (S, S, S), ((S, S),), 'broadcast_rhs'),
    ('mul', (S, S), ((S, S, S),), 'broadcast_lhs'),
    ('mul', (S, 1, S), ((M, S),), 'broadcast_all'),
    ('mul', (S, S, S), (3.14,), 'constant'),
    ('__rmul__', (S, S, S), (3.14,), 'constant'),
    ('div', (S, S, S), (torch.rand(S, S, S) + 0.1,)),
    ('div', (S, S, S), (torch.rand(S, S) + 0.1,), 'broadcast_rhs'),
    ('div', (S, S), (torch.rand(S, S, S) + 0.1,), 'broadcast_lhs'),
    ('div', (S, 1, S), (torch.rand(M, S) + 0.1,), 'broadcast_all'),
    ('div', torch.rand(S, S, S) + 1e-1, (3.14,), 'constant'),
    ('__rdiv__', torch.rand(S, S, S) + 1e-1, (3.14,), 'constant'),
    ('pow', torch.rand(S, S, S) + 1e-3, (torch.rand(S, S, S) + 0.1,)),
    ('pow', torch.rand(S, S, S) + 1e-3, (torch.rand(1,) + 0.1,), 'broadcast_rhs'),
    ('pow', torch.rand(1,) + 1e-3, (torch.rand(S, S, S) + 0.1,), 'broadcast_lhs'),
    ('pow', torch.rand(S, 1, S) + 1e-3, (torch.rand(1, S, 1) + 0.1,), 'broadcast_all'),
    ('pow', torch.rand(S, S, S) + 1e-3, (3.14,), 'constant'),
    ('__rpow__', torch.rand(S, S, S) + 1e-3, (3.14,), 'constant'),
    ('transpose', (1, 2, 3), (1, 2), 'dim', [0, 1]),
    ('transpose', torch.rand(L, L), (0, 1), '2d'),
    ('transpose', torch.rand(S, S, S), (2, 0), '3d'),
    ('t', (1, 2), ()),
    ('view', (S, S, S), (S * S, S),),
    ('view', (S, S, S), (torch.Size([S * S, S]),), 'size'),
    ('view', (S,), (S,), '1d'),
    ('view_as', (S, S, S), (Variable(torch.rand(S * S, S), requires_grad=False),)),
    ('expand', (S, 1, 1), (S, S, S)),
    ('expand', (torch.Size([S, 1, S]),), (S, S, S), 'size'),
    ('expand', (S, 1), (S, S, S), 'new_dim'),
    ('expand', (1,), (S, S, S), 'scalar'),
    ('expand', (1, S), (1, 1, S), 'new_dim_front_old_front_1'),
    ('exp', (S, S, S), ()),
    ('erf', torch.rand(S, S, S), ()),
    ('erfinv', torch.rand(S, S, S).clamp(-0.9, 0.9), ()),
    ('log', torch.rand(S, S, S) + 1e-2, ()),
    ('log1p', torch.rand(S, S, S), ()),
    ('tanh', (S, S, S), ()),
    ('sigmoid', (S, S, S), ()),
    ('sinh', (S, S, S), ()),
    ('cosh', (S, S, S), ()),
    ('abs', (S, S, S), ()),
    ('clamp', (S, S, S), (0, 1)),
    ('clamp', (S, S, S), (None, 0.5), 'min'),
    ('clamp', (S, S, S), (0.5, None), 'max'),
    ('sqrt', torch.rand(S, S, S) + 5e-4, ()),
    ('sin', (S, S, S), ()),
    ('cos', (S, S, S), ()),
    ('tan', torch.randn(S, S, S).clamp(-1, 1), ()),
    ('asin', torch.randn(S, S, S).clamp(-0.9, 0.9), ()),
    ('acos', torch.randn(S, S, S).clamp(-0.9, 0.9), ()),
    ('atan', (S, S, S), ()),
    ('atan2', (S, S, S), ((S, S, S),)),
    ('reciprocal', torch.rand(S, S, S) + 0.1, ()),
    ('round', (S, S, S), ()),
    ('sign', (S, S, S), ()),
    ('trunc', (S, S, S), ()),
    ('floor', (S, S, S), ()),
    ('ceil', (S, S, S), ()),
    ('rsqrt', torch.rand(S, S, S) + 1e-2, ()),
    ('frac', (S, S, S), ()),
    ('fmod', (S, S, S), (1.5,)),
    ('fmod', (S, S, S), (Variable(torch.rand(S, S, S) + 1.5, requires_grad=False),), 'tensor'),
    ('fmod', (S,), (Variable(torch.rand(S, S, S) + 1.5, requires_grad=False),), 'tensor_broadcast_lhs'),
    ('fmod', (S, S, S), (Variable(torch.rand(S) + 1.5, requires_grad=False),), 'tensor_broadcast_rhs'),
    ('fmod', (S, 1, S), (Variable(torch.rand(S, S) + 1.5, requires_grad=False),), 'tensor_broadcast_all'),
    ('remainder', (S, S, S), (1.5,)),
    ('remainder', (S, S, S), (Variable(torch.rand(S, S, S) + 1.5, requires_grad=False),), 'tensor'),
    ('remainder', (S,), (Variable(torch.rand(S, S, S) + 1.5, requires_grad=False),), 'tensor_broadcast_lhs'),
    ('remainder', (S, 1, S), (Variable(torch.rand(S, S) + 1.5, requires_grad=False),), 'tensor_broadcast_all'),
    ('lerp', (S, S, S), ((S, S, S), 0.4)),
    ('lerp', (S, S, S), ((S,), 0.4), 'broadcast_rhs'),
    ('lerp', (S,), ((S, S, S), 0.4), 'broadcast_lhs'),
    ('lerp', (S, 1, S), ((S, S), 0.4), 'broadcast_all'),
    ('max', (S, S, S), ()),
    ('max', (S, S, S), (1,), 'dim', [0]),
    ('max', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('max', (S,), (0,), 'dim_1d', [0]),
    ('max', (S,), (0, True,), 'keepdim_dim_1d', [0]),
    ('max', (S, S, S), ((S, S, S),), 'elementwise'),
    ('max', (S, S, S), ((S,),), 'elementwise_broadcast_rhs'),
    ('max', (S,), ((S, S, S),), 'elementwise_broadcast_lhs'),
    ('max', (S, 1, S), ((S, S),), 'elementwise_broadcast_all'),
    ('min', (S, S, S), ()),
    ('min', (S, S, S), (1,), 'dim', [0]),
    ('min', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('min', (S,), (0,), 'dim_1d', [0]),
    ('min', (S,), (0, True,), 'keepdim_dim_1d', [0]),
    ('min', (S, S, S), ((S, S, S),), 'elementwise'),
    ('min', (S, S, S), ((S,),), 'elementwise_broadcast_rhs'),
    ('min', (S,), ((S, S, S),), 'elementwise_broadcast_lhs'),
    ('min', (S, 1, S), ((S, S),), 'elementwise_broadcast_all'),
    ('mean', (S, S, S), ()),
    ('mean', (S, S, S), (1,), 'dim', [0]),
    ('mean', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('mean', (S,), (0,), 'dim_1d', [0]),
    ('mean', (S,), (0, True), 'keepdimdim_1d', [0]),
    ('kthvalue', (S, S, S), (2,)),
    ('kthvalue', (S, S, S), (2, 1,), 'dim', [1]),
    ('kthvalue', (S, S, S), (2, 1, True,), 'keepdim_dim', [1]),
    ('kthvalue', (S,), (2, 0,), 'dim_1d', [1]),
    ('kthvalue', (S,), (2, 0, True,), 'keepdim_dim_1d', [1]),
    ('median', (S, S, S), ()),
    ('median', (S, S, S), (1,), 'dim', [0]),
    ('median', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('median', (S,), (0,), 'dim_1d', [0]),
    ('median', (S,), (0, True,), 'keepdim_dim_1d', [0]),
    ('mode', (S, S, S), ()),
    ('mode', (S, S, S), (1,), 'dim', [0]),
    ('mode', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('mode', (S,), (0,), 'dim_1d', [0]),
    ('mode', (S,), (0, True,), 'keepdim_dim_1d', [0]),
    ('sum', (S, S, S), ()),
    ('sum', (S, S, S), (1,), 'dim', [0]),
    ('sum', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('sum', (S,), (0,), 'dim_1d', [0]),
    ('sum', (S,), (0, True), 'keepdim_1d', [0]),
    ('prod', (S, S, S), ()),
    ('prod', (S, S, S), (1,), 'dim', [0]),
    ('prod', (S, S, S), (1, True,), 'keepdim_dim', [0]),
    ('prod', (S,), (0,), 'dim_1d', [0]),
    ('prod', (S,), (0, True), 'keepdim_1d', [0]),
    ('prod', prod_zeros(S, [0, 1]), (), 'zerodims2'),
    ('prod', prod_zeros(S, [0, 2]), (), 'zerodims1'),
    ('prod', prod_zeros(S, [1, 2]), (), 'zerodims0'),
    ('prod', prod_zeros(S, [0, 1]), (1,), 'zeros_dims2', [0]),
    ('prod', prod_zeros(S, [0, 2]), (1,), 'zeros_dims1', [0]),
    ('prod', prod_zeros(S, [1, 2]), (1,), 'zeros_dims0', [0]),
    ('prod', prod_zeros(S, [0, 1]), (1, True), 'keepdim_zeros_dims2', [0]),
    ('prod', prod_zeros(S, [0, 2]), (1, True), 'keepdim_zeros_dims1', [0]),
    ('prod', prod_zeros(S, [1, 2]), (1, True), 'keepdim_zeros_dims0', [0]),
    ('prod', prod_single_zero(S), (), 'single_zero'),
    ('var', (S, S, S), ()),
    ('var', (S, S, S), (1,), 'dim', [0]),
    ('var', (S, S, S), (1, True, True), 'keepdim_dim', [0]),
    ('var', (S,), (0,), 'dim_1d', [0]),
    ('var', (S,), (0, True, True), 'keepdim_dim_1d', [0]),
    ('std', (S, S, S), ()),
    ('std', (S, S, S), (1,), 'dim', [0]),
    ('std', (S, S, S), (1, True, True), 'keepdim_dim', [0]),
    ('std', (S,), (0,), 'dim_1d', [0]),
    ('std', (S,), (0, True, True), 'keepdim_dim_1d', [0]),
    ('renorm', (S, S, S), (2, 1, 0.5), 'dim', [1]),
    ('renorm', (S, S, S), (1, 2, 3), 'norm_1'),
    ('repeat', (S, S, S, S), (2, 3, 1, 4)),
    ('repeat', (S, S, S, S), (2, 2, 1, 3, 1, 2), 'unsqueeze'),
    ('cumsum', (S, S, S), (1,), 'dim0', [0]),
    ('cumsum', (S, S, S), (1,), 'dim1', [0]),
    ('cumsum', (S,), (0,), '1d', [0]),
    ('cumprod', (S, S, S), (0,)),
    ('cumprod', (S, S, S), (1,), 'dim1', [0]),
    ('cumprod', (S,), (0,), '1d'),
    ('cumprod', prod_zeros(S, [0, 1]), (1,), 'zeros_dim2', [0]),
    ('cumprod', prod_zeros(S, [0, 2]), (1,), 'zeros_dim1', [0]),
    ('cumprod', prod_zeros(S, [1, 2]), (1,), 'zeros_dim0', [0]),
    ('unfold', (S, S, S, S), (1, 3, 1)),
    ('unfold', (S, S, S), (2, 3, 2), 'lastdim'),
    ('addmm', (S, M), ((S, S), (S, M)),),
    ('addmm', (1,), ((S, S), (S, M)), 'broadcast_lhs'),
    ('addmm', (S, M), (0.2, 0.6, (S, S), (S, M)), 'coef'),
    ('addmm', (1,), (0.2, 0.6, (S, S), (S, M)), 'broadcast_lhs_coef'),
    ('addbmm', (S, M), ((S, S, S), (S, S, M)),),
    ('addbmm', (1,), ((S, S, S), (S, S, M)), 'broadcast_lhs'),
    ('addbmm', (S, M), (0.2, 0.6, (S, S, S), (S, S, M)), 'coef'),
    ('addbmm', (1,), (0.2, 0.6, (S, S, S), (S, S, M)), 'broadcast_lhs_coef'),
    ('baddbmm', (S, S, M), ((S, S, S), (S, S, M)),),
    ('baddbmm', (1,), ((S, S, S), (S, S, M)), 'broadcast_lhs'),
    ('baddbmm', (S, S, M), (0.2, 0.6, (S, S, S), (S, S, M)), 'coef'),
    ('baddbmm', (1,), (0.2, 0.6, (S, S, S), (S, S, M)), 'broadcast_lhs_coef'),
    ('addmv', (S,), ((S, M), (M,)),),
    ('addmv', (1,), ((S, M), (M,)), 'broadcast_lhs'),
    ('addmv', (S,), (0.2, 0.6, (S, M), (M,)), 'coef'),
    ('addmv', (1,), (0.2, 0.6, (S, M), (M,)), 'broadcast_lhs_coef'),
    ('addr', (S, M), ((S,), (M,)),),
    ('addr', (1,), ((S,), (M,)), 'broadcast_lhs'),
    ('addr', (S, M), (0.2, 0.6, (S,), (M,)), 'coef'),
    ('addr', (1,), (0.2, 0.6, (S,), (M,)), 'broadcast_lhs_coef'),
    ('dot', (L,), ((L,),),),
    ('mm', (S, M), ((M, S),)),
    ('bmm', (M, S, M), ((M, M, S),)),
    ('mv', (S, M), ((M,),)),
    ('ger', (S,), ((M,),)),
    ('matmul', (L,), ((L,),),),
    ('matmul', (S, M), ((M,),), "2d_1d"),
    ('matmul', (M, ), ((M, S),), "1d_2d"),
    ('matmul', (S, M), ((M, S),), "2d_2d"),
    ('matmul', (S, S, M, M), ((S, S, M, S),), "4d_4d"),
    ('matmul', (S, S, M, M), ((M,),), "4d_1d"),
    ('matmul', (M,), ((S, S, M, S),), "1d_4d"),
    ('addcmul', (S, S), ((S, S), (S, S))),
    ('addcmul', (S, S), ((S, 1), (1, S)), 'broadcast_rhs'),
    ('addcmul', (1,), ((S, S, 1), (1, S)), 'broadcast_all'),
    ('addcmul', (S, S), (0.5, (S, S), (S, S)), 'scale'),
    ('addcmul', (S, S), (0.5, (S, 1), (1, S)), 'scale_broadcast_rhs'),
    ('addcmul', (1,), (0.5, (S, S, 1), (1, S)), 'scale_broadcast_all'),
    ('addcdiv', (S, S), ((S, S), (S, S))),
    ('addcdiv', (S, S), ((S, 1), (1, S)), 'broadcast_rhs'),
    ('addcdiv', (1,), ((S, S, 1), (1, S)), 'broadcast_all'),
    ('addcdiv', (S, S), (0.5, (S, S), (S, S)), 'scale'),
    ('addcdiv', (S, S), (0.5, (S, 1), (1, S)), 'scale_broadcast_rhs'),
    ('addcdiv', (1,), (0.5, (S, S, 1), (1, S)), 'scale_broadcast_all'),
    ('zero_', (S, S, S), ()),
    ('norm', (S, S, S), (2,)),
    ('norm', (S, S, S), (3,), '3'),
    ('norm', torch.rand(S, S, S) + 5e-2, (1.5,), '1_5'),
    ('norm', (S, S, S), (2, 1), '2_dim', [1]),
    ('norm', (S, S, S), (3, 1), '3_dim', [1]),
    ('norm', torch.rand(S, S, S) + 5e-2, (1.5, 1), '1_5_dim', [1]),
    ('norm', (S, S, S), (2, 1, True), 'keepdim_2_dim', [1]),
    ('norm', (S, S, S), (3, 1, True), 'keepdim_3_dim', [1]),
    ('norm', torch.rand(S, S, S) + 5e-2, (1.5, 1, True), 'keepdim_1_5_dim', [1]),
    ('norm', (S,), (2, 0), '2_dim_1d', [1]),
    ('norm', (S,), (3, 0), '3_dim_1d', [1]),
    ('norm', (S,), (2, 0, True), 'keepdim_2_dim_1d', [1]),
    ('norm', (S,), (3, 0, True), 'keepdim_3_dim_1d', [1]),
    ('clone', (S, M, S), ()),
    ('dist', (S, S, S), ((S, S, S),)),
    ('dist', (S, S, S), ((S,),), 'broadcast_rhs'),
    ('dist', (S,), ((S, S, S),), 'broadcast_lhs'),
    ('dist', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('dist', (S, S, S), ((S, S, S), 4), '4'),
    ('dist', (S, S, S), ((S,), 4), '4_broadcast_rhs'),
    ('dist', (S,), ((S, S, S), 4), '4_broadcast_lhs'),
    ('dist', (S, 1, S), ((S, S), 4), '4_broadcast_all'),
    ('diag', (M, M), (), '2d'),
    ('diag', (M,), (), '1d'),
    ('diag', (M, M), (1,), '2d_1'),
    ('diag', (M, M), (2,), '2d_2'),
    ('tril', (M, M), ()),
    ('tril', (M, M), (2,), 'idx'),
    ('triu', (M, M), ()),
    ('triu', (M, M), (2,), 'idx'),
    ('trace', (M, M), ()),
    ('cross', (S, 3), ((S, 3),)),
    ('cross', (S, 3, S), ((S, 3, S), 1), 'dim'),
    ('index_select', (S, S, S), (0, index_variable(2, S)), 'dim', [0]),
    ('index_add', (S, S), (0, index_variable(2, S), (2, S)), 'dim', [0]),
    ('index_copy', (S, S), (0, index_perm_variable(2, S), (2, S)), 'dim', [0]),
    ('index_fill', (S, S), (0, index_variable(2, S), 2), 'dim', [0]),
    ('inverse', (S, S), (), '', (), [skipIfNoLapack]),
    ('gesv', (S, S), ((S, S),), '', (), [skipIfNoLapack]),
    ('potrf', _make_cov(S), (True,), '', (), [skipIfNoLapack]),
    ('eq', (S, S, S), ((S, S, S),)),
    ('eq', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('eq', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('eq', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('ne', (S, S, S), ((S, S, S),)),
    ('ne', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('ne', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('ne', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('gt', (S, S, S), ((S, S, S),)),
    ('gt', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('gt', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('gt', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('ge', (S, S, S), ((S, S, S),)),
    ('ge', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('ge', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('ge', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('lt', (S, S, S), ((S, S, S),)),
    ('lt', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('lt', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('lt', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('le', (S, S, S), ((S, S, S),)),
    ('le', (S, S, S), ((1,),), 'broadcast_rhs'),
    ('le', (1,), ((S, S, S),), 'broadcast_lhs'),
    ('le', (S, 1, S), ((S, S),), 'broadcast_all'),
    ('eq', (S, S, S), (0,), 'scalar'),
    ('ne', (S, S, S), (0,), 'scalar'),
    ('gt', (S, S, S), (0,), 'scalar'),
    ('ge', (S, S, S), (0,), 'scalar'),
    ('lt', (S, S, S), (0,), 'scalar'),
    ('le', (S, S, S), (0,), 'scalar'),
    ('permute', (1, 2, 3, 4), (0, 2, 3, 1)),
    ('select', (S, S, S), (1, 2), 'dim', [0]),
    ('narrow', (S, S, S), (1, 2, 2), 'dim', [0]),
    ('_unnarrow', (S, S, S), (0, 2, M), 'dim', [0]),
    ('squeeze', (S, 1, S, 1), ()),
    ('squeeze', (S, 1, S, 1), (1,), '1_dim', [0]),
    ('squeeze', (S, 1, S, 1), (2,), 'not_1_dim', [0]),
    ('unsqueeze', (S, S, S), (0,), 'first', [0]),
    ('unsqueeze', (S, S, S), (1,), 'middle', [0]),
    ('unsqueeze', (S, S, S), (3,), 'last', [0]),
    ('gather', (M, S), (0, gather_variable((S, S), 1, M, True)), 'dim0', [0]),
    ('gather', (M, S), (1, gather_variable((M, S // 2), 0, S, True)), 'dim1', [0]),
    ('scatter', (M, S), (0, gather_variable((S, S), 1, M), (S, S)), 'dim0', [0]),
    ('scatter', (M, S), (1, gather_variable((M, S // 2), 0, S), (M, S // 2)), 'dim1', [0]),
    ('scatter_add', (M, S), (0, gather_variable((S, S), 1, M), (S, S)), 'dim0', [0]),
    ('scatter_add', (M, S), (1, gather_variable((M, S // 2), 0, S), (M, S // 2)), 'dim1', [0]),
    ('masked_select', (M, M), (Variable(mask_not_all_zeros((M, M)), requires_grad=False),)),
    ('masked_select', (M, M), (Variable(mask_not_all_zeros((M,)), requires_grad=False),), 'broadcast_rhs'),
    ('masked_select', (M,), (Variable(mask_not_all_zeros((M, M)), requires_grad=False),), 'broadcast_lhs'),
    ('masked_select', (M, 1, M), (Variable(mask_not_all_zeros((M, M)), requires_grad=False),),
     'broadcast_all'),
    ('masked_fill', (M, M), (Variable(torch.ByteTensor(M, M).bernoulli_(), requires_grad=False), 10)),
    # no lhs or all broadcast on masked_fill or masked_scatter because it's always inplace
    ('masked_fill', (M, M), (Variable(torch.ByteTensor(M,).bernoulli_(), requires_grad=False), 10), 'broadcast_rhs'),
    ('masked_scatter', (M, M), (Variable(torch.ByteTensor(M, M).bernoulli_(), requires_grad=False), (M, M))),
    ('masked_scatter', (M, M), (Variable(torch.ByteTensor(M,).bernoulli_(), requires_grad=False), (M, M)),
     'broadcast_rhs'),
    ('resize', (S, S, S), (torch.Size([S * S, S])), 'fewer_dims'),
    ('resize_as', (S, S, S), (Variable(torch.randn((S * S, S)), requires_grad=False),)),
    ('sort', (S, M, S), ()),
    ('sort', (S, M, S), (1,), 'dim'),
    ('sort', (S, M, S), (1, True), 'dim_desc'),
    ('topk', (S, M, S), (3,)),
    ('topk', (S, M, S), (3, 1), 'dim'),
    ('topk', (S, M, S), (3, 1, True), 'dim_desc'),
    ('topk', (S, M, S), (3, 1, True, True), 'dim_desc_sort'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([1, 2]),)),
    ('__getitem__', torch.randn(S, S, S), (slice(0, 3),), 'slice'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([slice(0, 3), 1]),), 'slice_index'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 2, 3], [1, 3, 3], [0, 0, 2]]),), 'adv_index'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 0, 3], [1, 1, 3], [0, 0, 2]]),), 'adv_index_dup'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([slice(None), slice(None), [0, 3]]),), 'adv_index_end'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([slice(None), [0, 3], slice(None)]),), 'adv_index_mid'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 3], slice(None), slice(None)]),), 'adv_index_beg'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 3], [1, 2], slice(None)]),), 'adv_index_comb'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 3], ]),), 'adv_index_sub'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 3], slice(None)]),), 'adv_index_sub_2'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 3], Ellipsis]),), 'adv_index_sub_3'),
    ('__getitem__', torch.randn(S, S, S), (dont_convert([[0, 2, 3], [1, 3, 3],
     Variable(torch.LongTensor([0, 0, 2]), requires_grad=False)]),), 'adv_index_var'),
]
# TODO: clamp with min/max


def make_non_contiguous(tensor):
    osize = list(tensor.size())

    # randomly inflate a few dimensions in osize
    for _ in range(2):
        dim = random.randint(0, len(osize) - 1)
        add = random.randint(4, 15)
        osize[dim] = osize[dim] + add

    # narrow doesn't make a non-contiguous tensor if we only narrow the 0-th dimension,
    # (which will always happen with a 1-dimensional tensor), so let's make a new
    # right-most dimension and cut it off

    input = tensor.new(torch.Size(osize + [random.randint(2, 3)]))
    input = input.select(len(input.size()) - 1, random.randint(0, 1))
    # now extract the input of correct size from 'input'
    for i in range(len(osize)):
        if input.size(i) != tensor.size(i):
            bounds = random.randint(1, input.size(i) - tensor.size(i))
            input = input.narrow(i, bounds, tensor.size(i))

    input.copy_(tensor)
    return input


def create_input(call_args, requires_grad=True, non_contiguous=False):
    if not isinstance(call_args, tuple):
        call_args = (call_args,)

    def map_arg(arg):
        def maybe_non_contig(tensor):
            return tensor if not non_contiguous else make_non_contiguous(tensor)

        if isinstance(arg, torch.Size) or isinstance(arg, dont_convert):
            return arg
        elif isinstance(arg, tuple) and not isinstance(arg[0], Variable):
            return Variable(maybe_non_contig(torch.randn(*arg).double()), requires_grad=requires_grad)
        elif torch.is_tensor(arg):
            if isinstance(arg, torch.FloatTensor):
                return Variable(maybe_non_contig(arg.double()), requires_grad=requires_grad)
            else:
                return Variable(maybe_non_contig(arg), requires_grad=requires_grad)
        elif isinstance(arg, Variable) and non_contiguous:
            return Variable(maybe_non_contig(arg.data), requires_grad=arg.requires_grad)
        else:
            return arg
    return tuple(map_arg(arg) for arg in call_args)


def unpack_variables(args):
    if isinstance(args, Variable):
        return args.data
    elif isinstance(args, tuple):
        return tuple(unpack_variables(elem) for elem in args)
    else:
        return args


def generate_gradoutput(dummy_out, non_contiguous=False):
    def maybe_non_contig(tensor):
        return tensor if not non_contiguous else make_non_contiguous(tensor)

    if isinstance(dummy_out, tuple):
        grad_y = tuple(Variable(maybe_non_contig(torch.randn(x.size())), requires_grad=x.requires_grad)
                       for x in dummy_out if isinstance(x, Variable))
    else:
        grad_y = (Variable(maybe_non_contig(torch.randn(dummy_out.size())), requires_grad=dummy_out.requires_grad),)

    return grad_y

EXCLUDE_FUNCTIONAL = {
    'addmm',
    'addbmm',
    'baddbmm',
    'addmv',
    'addr',
}
EXCLUDE_GRADCHECK = {
    'potrf'
}


def exclude_tensor_method(name, test_name):
    # there are no tensor equivalents for these (inplace or out)
    exclude_all_tensor_method_by_test_name = {
        'test_clamp_min',
        'test_clamp_max',
        'test__unnarrow_dim',
        'test__unnarrow_dim_neg0',
    }
    # there are no out-of-place tensor equivalents for these
    exclude_outplace_tensor_method = {
        'index_add',
        'index_copy',
        'index_fill',
        'masked_fill',
        'masked_scatter',
        'resize',
        'resize_as',
        'scatter',
        'scatter_add',
    }
    if test_name in exclude_all_tensor_method_by_test_name:
        return True
    is_magic_method = name[:2] == '__' and name[-2:] == '__'
    is_inplace = name[-1] == "_" and not is_magic_method
    if not is_inplace and name in exclude_outplace_tensor_method:
        return True
    return False


def gradgradcheck_method_precision_override(test_name):
    # these are just empirical observations, we should improve
    gradgradcheck_precision_override = {
        'test_norm': {'atol': 2e-2, 'rtol': 1e-2},
        'test_norm_1_5': {'atol': 1.5e-2, 'rtol': 1e-2},
        'test_norm_3': {'atol': 5e-2, 'rtol': 1e-2},
        'test_dist': {'atol': 5e-2, 'rtol': 1e-2},
        'test_dist_4': {'atol': 8e-2, 'rtol': 1e-2},
    }
    non_broadcasted_test_name = test_name.split("_broadcast")[0]
    override = gradgradcheck_precision_override.get(non_broadcasted_test_name)
    if override:
        if 'broadcast_lhs' in test_name or 'broadcast_rhs' in test_name:
            # errors accumulated across 1 dimension
            override = {'atol': override['atol'] * S, 'rtol': override['atol'] * S}
        elif 'broadcast_all' in test_name:
            # errors accumulated across multiple dimensions
            override = {'atol': override['atol'] * S * S, 'rtol': override['atol'] * S * S}
    return override


def run_grad_and_gradgrad_checks(test_case, test_name, apply_method, output_variable, input_variables):
    test_case.assertTrue(gradcheck(apply_method, input_variables, eps=1e-6, atol=PRECISION))

    grad_y = generate_gradoutput(output_variable, non_contiguous=True)
    gradgradcheck_precision_override = gradgradcheck_method_precision_override(test_name)
    if gradgradcheck_precision_override is not None:
        atol = gradgradcheck_precision_override['atol']
        rtol = gradgradcheck_precision_override['rtol']
        test_case.assertTrue(gradgradcheck(apply_method, input_variables, grad_y, atol=atol, rtol=rtol))
    else:
        test_case.assertTrue(gradgradcheck(apply_method, input_variables, grad_y,))


def run_functional_checks(test_case, test_name, name, apply_fn, run_grad_checks,
                          f_args_variable, f_args_tensor):
    output_variable = apply_fn(*f_args_variable)
    if not exclude_tensor_method(name, test_name):
        output_tensor = apply_fn(*f_args_tensor)
        if not torch.is_tensor(output_tensor) and not isinstance(output_tensor, tuple):
            output_tensor = torch.DoubleTensor((output_tensor,))
        test_case.assertEqual(unpack_variables(output_variable), output_tensor)

    if run_grad_checks:
        run_grad_and_gradgrad_checks(test_case, test_name, apply_fn,
                                     output_variable, f_args_variable)

    self_variable = f_args_variable[0]
    if isinstance(output_variable, torch.autograd.Variable) and self_variable is not None:
        output_variable.backward(torch.randn(*output_variable.size()).type_as(output_variable.data))
        test_case.assertTrue(type(self_variable.data) == type(self_variable.grad.data))
        test_case.assertTrue(self_variable.size() == self_variable.grad.size())

for test in method_tests:
    name, self_size, args = test[:3]
    basic_test_name = 'test_' + name
    if len(test) >= 4 and test[3] != '':
        basic_test_name += '_' + test[3]

    dim_args_idx = test[4] if len(test) == 5 else []

    skipTestIf = test[5] if len(test) == 6 else []

    for dim_perm in product([-1, 1], repeat=len(dim_args_idx)):
        test_name = basic_test_name
        new_args = [arg * dim_perm[dim_args_idx.index(i)] if i in dim_args_idx else arg for i, arg in enumerate(args)]
        test_name = basic_test_name + ''.join('_neg' + str(i) for i, idx in enumerate(dim_perm) if idx < 0)
        new_args = tuple(new_args)

        # for-loop bodies don't define scopes, so we have to save the variables
        # we want to close over in some way
        def do_test(self, name=name, self_size=self_size, args=new_args, test_name=test_name):
            def check(name):
                is_magic_method = name[:2] == '__' and name[-2:] == '__'
                is_inplace = name[-1] == "_" and not is_magic_method
                self_variable = create_input((self_size,), requires_grad=not is_inplace)[0]
                args_variable = create_input(args, requires_grad=not is_inplace)
                self_tensor = deepcopy(self_variable.data)
                args_tensor = deepcopy(unpack_variables(args_variable))
                output_variable = getattr(self_variable, name)(*args_variable)
                if not exclude_tensor_method(name, test_name):
                    output_tensor = getattr(self_tensor, name)(*args_tensor)
                    if not torch.is_tensor(output_tensor) and not isinstance(output_tensor, tuple):
                        output_tensor = torch.DoubleTensor((output_tensor,))
                    self.assertEqual(unpack_variables(output_variable), output_tensor)
                    # TODO: check that both have changed after adding all inplace ops

                if not is_inplace and name not in EXCLUDE_GRADCHECK:
                    run_grad_and_gradgrad_checks(self, test_name,
                                                 lambda *inputs: getattr(inputs[0], name)(*inputs[1:]),
                                                 output_variable, (self_variable,) + args_variable)

                # functional interface tests
                if hasattr(torch, name) and name not in EXCLUDE_FUNCTIONAL:
                    f_args_variable = (self_variable,) + args_variable
                    f_args_tensor = (self_tensor,) + args_tensor
                    # could run the gradchecks again, but skip since we did it for the methods above.
                    run_functional_checks(self, test_name, name,
                                          lambda *inputs: getattr(torch, name)(*inputs),
                                          False, f_args_variable, f_args_tensor)

                # check for correct type of input.data and input.grad.data
                if not is_inplace:
                    self_variable = create_input((self_size,), requires_grad=True)[0]
                    args_variable = create_input(args, requires_grad=False)
                    output_variable = getattr(self_variable, name)(*args_variable)
                    if isinstance(output_variable, torch.autograd.Variable):
                        output_variable.backward(torch.randn(*output_variable.size()).type_as(output_variable.data))
                        self.assertTrue(type(self_variable.data) == type(self_variable.grad.data))
                        self.assertTrue(self_variable.size() == self_variable.grad.size())

                    # compare grads to inplace grads
                    inplace_name = name + '_'
                    # can't broadcast inplace to left hand side
                    broadcast_skip_inplace = 'broadcast_lhs' in test_name or 'broadcast_all' in test_name
                    if hasattr(Variable(torch.ones(1)), inplace_name) and not broadcast_skip_inplace:
                        output_variable = getattr(self_variable, name)(*args_variable)
                        if not isinstance(output_variable, tuple):
                            output_variable = (output_variable,)
                        inplace_self_variable = deepcopy(self_variable)
                        inplace_self_variable_copy = tuple(i + 0 if i is not None else None
                                                           for i in (inplace_self_variable,))
                        inplace_args_variable = deepcopy(args_variable)
                        inplace_args_variable_copy = tuple(i + 0 if i is not None else None
                                                           for i in inplace_args_variable)

                        try:
                            inplace_output_variable = (
                                getattr(inplace_self_variable_copy[0], inplace_name)(*inplace_args_variable_copy))
                        except RuntimeError as err:
                            if 'only supports scalar multiplication' in str(err):
                                return
                            raise
                        if not isinstance(inplace_output_variable, tuple):
                            inplace_output_variable = (inplace_output_variable,)
                        self.assertEqual(inplace_output_variable, output_variable)
                        # Check that gradient is the same
                        for inp_i, i in zip((inplace_self_variable,) + inplace_args_variable,
                                            (self_variable,) + args_variable):
                            if not isinstance(inp_i, Variable):
                                assert not isinstance(i, Variable)
                                continue
                            if inp_i.grad is not None:
                                inp_i.grad.data.zero_()
                            if i.grad is not None:
                                i.grad.data.zero_()
                        for io, o in zip(inplace_output_variable, output_variable):
                            grad = torch.randn(*io.size()).double()
                            io.backward(grad)
                            o.backward(grad)
                        for inp_i, i in zip((inplace_self_variable,) + inplace_args_variable,
                                            (self_variable,) + args_variable):
                            if not isinstance(inp_i, Variable):
                                continue
                            self.assertEqual(inp_i.grad, i.grad)

            check(name)
            inplace_name = name + '_'
            # can't broadcast inplace to left hand side
            broadcast_skip_inplace = 'broadcast_lhs' in test_name or 'broadcast_all' in test_name
            if hasattr(Variable(torch.ones(1)), inplace_name) and not broadcast_skip_inplace:
                try:
                    check(inplace_name)
                except Exception as e:
                    if 'only supports scalar' not in e.args[0]:
                        raise

        assert not hasattr(TestAutograd, test_name), 'Two tests have the same name: ' + test_name

        for skip in skipTestIf:
            do_test = skip(do_test)

        setattr(TestAutograd, test_name, do_test)


if __name__ == '__main__':
    run_tests()
