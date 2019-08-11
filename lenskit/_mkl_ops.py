import os
import pathlib
import logging

import cffi
from distutils import ccompiler

import numpy as np

from .matrix import CSR

_logger = logging.getLogger(__name__)
__dir = pathlib.Path(__file__).parent

def _compile_mkl_ops():
    from distutils import ccompiler
    cc = ccompiler.new_compiler()

    mkl_src = __dir / 'mkl_ops.c'
    mkl_obj = cc.object_filenames([os.fspath(mkl_src)])
    mkl_so = cc.shared_object_filename('mkl_ops')
    mkl_so = __dir / mkl_so

    if mkl_so.exists():
        src_mt = mkl_src.stat().st_mtime
        so_mt = mkl_so.stat().st_mtime
        if so_mt > src_mt:
            return mkl_so

    _logger.info('compiling MKL support library')
    i_dirs = []
    l_dirs = []
    if os.name == 'nt':
        lib = pathlib.Path(os.environ['CONDA_PREFIX']) / 'Library'
        i_dirs.append(os.fspath(lib / 'include'))
        l_dirs.append(os.fspath(lib / 'lib'))

    cc.compile([os.fspath(mkl_src)], include_dirs=i_dirs)
    cc.link_shared_object(mkl_obj, os.fspath(mkl_so), libraries=['mkl_rt'],
                          library_dirs=l_dirs)

    return mkl_so


__mkl_defs = (__dir / 'mkl_ops.h').read_text()
_mkl_op_ffi = cffi.FFI()
_mkl_op_ffi.cdef(__mkl_defs.replace('EXPORT ', ''))
_mkl_so = _compile_mkl_ops()
_mkl_op_lib = _mkl_op_ffi.dlopen(os.fspath(_mkl_so))


_mkl_errors = [
    'SPARSE_STATUS_SUCCESS',
    'SPARSE_STATUS_NOT_INITIALIZED',
    'SPARSE_STATUS_ALLOC_FAILED',
    'SPARSE_STATUS_INVALID_VALUE',
    'SPARSE_STATUS_EXECUTION_FAILED',
    'SPARSE_STATUS_INTERNAL_ERROR',
    'SPARSE_STATUS_NOT_SUPPORTED'
]


def _mkl_check_return(rv, call='<unknown>'):
    if rv:
        if rv >= 0 and rv < len(_mkl_errors):
            desc = _mkl_errors[rv]
        else:
            desc = 'unknown'
        raise RuntimeError('MKL call {} failed with code {} ({})'.format(call, rv, desc))


class SparseM:
    """
    Class encapsulating an MKL sparse matrix handle.
    """

    def __init__(self):
        self.ptr = None

    @classmethod
    def from_csr(cls, csr):
        """
        Create an MKL sparse matrix from a LensKit CSR matrix.

        Args:
            csr(CSR): the input matrix.

        Returns:
            SparseM: a sparse matrix handle for the CSR matrix.
        """
        sp = np.require(csr.rowptrs, np.intc, 'C')
        cols = np.require(csr.colinds, np.intc, 'C')
        vals = np.require(csr.values, np.float_, 'C')

        m = SparseM()
        _logger.debug('creating MKL matrix 0x%08x from %dx%d CSR',
                      id(m), csr.nrows, csr.ncols)
        _sp = _mkl_op_ffi.from_buffer('int[]', sp)
        _cols = _mkl_op_ffi.from_buffer('int[]', cols)
        _vals = _mkl_op_ffi.from_buffer('double[]', vals)
        m.ptr = _mkl_op_lib.lk_mkl_spcreate(csr.nrows, csr.ncols, _sp, _cols, _vals)
        if not m.ptr:
            raise RuntimeError('MKL matrix creation failed')

        return m

    def __del__(self):
        if self.ptr:
            _logger.debug('destroying MKL sparse matrix 0x%08x', id(self))
            _mkl_op_lib.lk_mkl_spfree(self.ptr)

    def export(self):
        """
        Export an MKL sparse matrix as a LensKit CSR.

        Returns:
            CSR: the LensKit matrix.
        """
        rvs = _mkl_op_lib.lk_mkl_spexport(self.ptr)
        if rvs.nrows < 0:
            raise RuntimeError('MKL sparse export failed')
        _logger.debug('exporting matrix with shape (%d, %d)', rvs.nrows, rvs.ncols)

        sp = np.frombuffer(_mkl_op_ffi.buffer(rvs.row_sp, rvs.nrows * _mkl_op_ffi.sizeof('int')), 'intc')
        ep = np.frombuffer(_mkl_op_ffi.buffer(rvs.row_ep, rvs.nrows * _mkl_op_ffi.sizeof('int')), 'intc')
        end = ep[-1]
        cis = np.frombuffer(_mkl_op_ffi.buffer(rvs.colinds, end * _mkl_op_ffi.sizeof('int')), 'intc')
        vs = np.frombuffer(_mkl_op_ffi.buffer(rvs.values, end * _mkl_op_ffi.sizeof('double')), 'float64')
        sizes = ep - sp
        nnz = np.sum(sizes)

        csr = CSR.empty((rvs.nrows, rvs.ncols), sizes)
        assert nnz == csr.nnz

        for i in range(rvs.nrows):
            rs, re = csr.row_extent(i)
            csr.colinds[rs:re] = cis[sp[i]:ep[i]]
            csr.values[rs:re] = vs[sp[i]:ep[i]]

        return csr

    def mult_vec(self, alpha, x, beta, y):
        """
        Compute :math:`\\alpha A x + \\beta y`, where :math:`A` is this matrix.
        """
        x = np.require(x, np.float64, 'C')
        yout = np.require(y, np.float64, 'C')
        if yout is y:
            yout = yout.copy()

        _x = _mkl_op_ffi.from_buffer('double[]', x)
        _y = _mkl_op_ffi.from_buffer('double[]', yout)

        rv = _mkl_op_lib.lk_mkl_spmv(alpha, self.ptr, _x, beta, _y)
        _mkl_check_return(rv, 'mkl_sparse_d_mv')

        return yout


def csr_syrk(csr: CSR):
    """
    Interface to the ``mkl_sparse_syrk`` routine, with necessary setup and conversion.
    """

    _logger.debug('syrk: processing %dx%d matrix (%d nnz)', csr.nrows, csr.ncols, csr.nnz)

    src = SparseM.from_csr(csr)

    _logger.debug('syrk: ordering matrix')
    rv = _mkl_op_lib.lk_mkl_sporder(src.ptr)
    _mkl_check_return(rv, 'mkl_sparse_order')

    _logger.debug('syrk: multiplying matrix')
    m2 = SparseM()
    m2.ptr = _mkl_op_lib.lk_mkl_spsyrk(src.ptr)
    if not m2.ptr:
        raise ValueError('SYRK failed')
    del src  # free a little memory

    _logger.debug('syrk: exporting matrix')
    result = m2.export()
    _logger.debug('syrk: received %dx%d matrix (%d nnz)',
                  result.nrows, result.ncols, result.nnz)
    return result
