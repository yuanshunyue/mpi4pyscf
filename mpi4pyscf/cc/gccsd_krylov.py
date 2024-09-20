#!/usr/bin/env python
# Copyright 2014-2020 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Zhi-Hao Cui <zhcui0408@gmail.com>
#

"""
MPI-GCCSD with real intergals using Newton-Krylov solver.

Usage: mpirun -np 2 python gccsd.py
"""

from functools import reduce
import math
import numpy as np
import scipy
import scipy.linalg as la
from scipy import optimize as opt
from scipy.sparse import linalg as spla

from pyscf import lib
from pyscf import scf
from pyscf.cc import gccsd

from mpi4pyscf.lib import logger
from mpi4pyscf.tools import mpi
from mpi4pyscf.cc.ccsd import (_task_location, _sync_, _pack_scf)
from mpi4pyscf.cc.gccsd import GGCCSD

comm = mpi.comm
rank = mpi.rank

einsum = lib.einsum
einsum_mv = lib.einsum 

# ZHC NOTE define max_abs to reduce cost and allow termination of 1st iteration 
def safe_max_abs(x):
    if np.isfinite(x).all():
        return max(np.max(x), abs(np.min(x)))
    else:
        return 1e+12

def remove_t2_abab(mycc, t2):
    """
    Set the ABAB block of t2 to 0, inplace change t2.
    """
    t2T = t2.transpose(2, 3, 0, 1)
    nvir_seg, nvir, nocc = t2T.shape[:3]
    ntasks = mpi.pool.size
    
    nvir_a = mycc.nvir_a
    nocc_a = mycc.nocc_a

    vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
    vloc0, vloc1 = vlocs[rank]
    assert vloc1 - vloc0 == nvir_seg
    if vloc0 < nvir_a:
        end = min(vloc1, nvir_a)
        t2T[:(end - vloc0), nvir_a:, :nocc_a, nocc_a:] = 0.0
        t2T[:(end - vloc0), nvir_a:, nocc_a:, :nocc_a] = 0.0
    if vloc1 > nvir_a:
        start = max(vloc0, nvir_a)
        t2T[(start - vloc0):, :nvir_a, :nocc_a, nocc_a:] = 0.0
        t2T[(start - vloc0):, :nvir_a, nocc_a:, :nocc_a] = 0.0

def remove_t2_aaaa_bbbb(mycc, t2):
    """
    Set the AAAA and BBBB blocks of t2 to 0, inplace change t2.
    """
    t2T = t2.transpose(2, 3, 0, 1)
    nvir_seg, nvir, nocc = t2T.shape[:3]
    ntasks = mpi.pool.size
    
    nvir_a = mycc.nvir_a
    nocc_a = mycc.nocc_a

    vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
    vloc0, vloc1 = vlocs[rank]
    assert vloc1 - vloc0 == nvir_seg
    if vloc0 < nvir_a:
        end = min(vloc1, nvir_a)
        t2T[:(end - vloc0), :nvir_a, :nocc_a, :nocc_a] = 0.0
    if vloc1 > nvir_a:
        start = max(vloc0, nvir_a)
        t2T[(start - vloc0):, nvir_a:, nocc_a:, nocc_a:] = 0.0

def remove_amps(mycc, t1, t2, t1_frozen_list=None, t2_frozen_list=None):
    """
    Set the blocks of amps to 0, inplace change t1 and t2.
    
    t1_frozen_list: [[o0, v0], ...]
    t2_frozen_list: [[o0, o1, v0, v1]]

    o and v can be 'a' or 'b'.
    """
    nvir_a = mycc.nvir_a
    nocc_a = mycc.nocc_a

    if t1_frozen_list is not None:
        t1T = t1.T
        nvir, nocc = t1T.shape
        dic_o = {'a': slice(0, nocc_a), 'b': slice(nocc_a, nocc)}
        dic_v = {'a': slice(0, nvir_a), 'b': slice(nvir_a, nvir)}
        for (o0, v0) in t1_frozen_list:
            t1T[dic_v[v0], dic_o[o0]] = 0.0
    
    if t2_frozen_list is not None:
        t2T = t2.transpose(2, 3, 0, 1)
        nvir_seg, nvir, nocc = t2T.shape[:3]
        ntasks = mpi.pool.size

        vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
        vloc0, vloc1 = vlocs[rank]
        assert vloc1 - vloc0 == nvir_seg
        
        dic_o = {'a': slice(0, nocc_a), 'b': slice(nocc_a, nocc)}
        dic_v = {'a': slice(0, nvir_a), 'b': slice(nvir_a, nvir)}
        dic_v_seg = {'a': None, 'b': None}
        if vloc0 < nvir_a:
            dic_v_seg['a'] = slice(0, min(vloc1, nvir_a) - vloc0)
        if vloc1 > nvir_a:
            dic_v_seg['b'] = slice(max(vloc0, nvir_a) - vloc0, nvir_seg)
        
        for (o0, o1, v0, v1) in t2_frozen_list:
            if dic_v_seg[v0] is not None:
                t2T[dic_v_seg[v0], dic_v[v1], dic_o[o0], dic_o[o1]] = 0.0

@mpi.parallel_call
def set_zero_amps(mycc):
    """
    Set blocks of amps to 0, based on spin channels.
    """
    mycc.remove_amps(mycc.t1, mycc.t2, t1_frozen_list=mycc.t1_frozen_list,
                     t2_frozen_list=mycc.t2_frozen_list)
    mycc.remove_amps(mycc.l1, mycc.l2, t1_frozen_list=mycc.t1_frozen_list,
                     t2_frozen_list=mycc.t2_frozen_list)

@mpi.parallel_call
def analyze_amps(mycc):
    """
    Analyze the blocks of amps, based on spin channels.
    """
    log = logger.new_logger(mycc)
    t1 = mycc.t1
    t2 = mycc.t2
    nvir_a = mycc.nvir_a
    nocc_a = mycc.nocc_a
    assert nocc_a is not None and nvir_a is not None

    log.info('*' * 79)
    log.info('Analyze amps based on spin channels.')
    log.info("Tx %27s %12s %12s",
             "i      a      j      b    ", "max_abs", "la.norm")
    log.info('-' * 79)

    t1T = t1.T
    nvir, nocc = t1T.shape
    dic_o = {'a': slice(0, nocc_a), 'b': slice(nocc_a, nocc)}
    dic_v = {'a': slice(0, nvir_a), 'b': slice(nvir_a, nvir)}
    
    labs_o = {'a': "a (p)", 'b': "b (h)"}
    labs_v = {'a': "a (h)", 'b': "b (p)"}

    for o0 in ['a', 'b']:
        for v0 in ['a', 'b']:
            block = t1T[dic_v[v0], dic_o[o0]]
            norm_max = safe_max_abs(block)
            norm_tot = la.norm(block)
            log.info("T1 %6s %6s %6s %6s %12.6f %12.6f",
                     labs_o[o0], labs_v[v0], "", "", norm_max, norm_tot)
    
    t2T = t2.transpose(2, 3, 0, 1)
    nvir_seg, nvir, nocc = t2T.shape[:3]
    ntasks = mpi.pool.size

    vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
    vloc0, vloc1 = vlocs[rank]
    assert vloc1 - vloc0 == nvir_seg
        
    dic_o = {'a': slice(0, nocc_a), 'b': slice(nocc_a, nocc)}
    dic_v = {'a': slice(0, nvir_a), 'b': slice(nvir_a, nvir)}
    dic_v_seg = {'a': None, 'b': None}
    if vloc0 < nvir_a:
        dic_v_seg['a'] = slice(0, min(vloc1, nvir_a) - vloc0)
    if vloc1 > nvir_a:
        dic_v_seg['b'] = slice(max(vloc0, nvir_a) - vloc0, nvir_seg)
    
    log.info('-' * 79)
    for o0 in ['a', 'b']:
        for v0 in ['a', 'b']:
            for o1 in ['a', 'b']:
                for v1 in ['a', 'b']:
                    if dic_v_seg[v0] is None:
                        norm_max = 0.0
                        norm_tot = 0.0
                    else:
                        block = t2T[dic_v_seg[v0], dic_v[v1], dic_o[o0], dic_o[o1]]
                        norm_max = safe_max_abs(block)
                        norm_tot = la.norm(block) ** 2
                    norm_max = comm.allreduce(norm_max, op=mpi.MPI.MAX)
                    norm_tot = (comm.allreduce(norm_tot)) ** 0.5
                    log.info("T2 %6s %6s %6s %6s %12.6f %12.6f",
                             labs_o[o0], labs_v[v0], labs_o[o1], labs_v[v1],
                             norm_max, norm_tot)
    log.info('*' * 79)

@mpi.parallel_call(skip_args=[1], skip_kwargs=['eris'])
def pre_kernel(mycc, eris=None, t1=None, t2=None, max_cycle=50, tol=1e-8,
               tolnormt=1e-6, verbose=None):
    """
    ao2mo, init_amps, gather vector, cycle = 0.
    """
    log = logger.new_logger(mycc, verbose)
    cput0 = (logger.process_clock(), logger.perf_counter())
    _sync_(mycc)

    eris = getattr(mycc, '_eris', None)
    if eris is None:
        mycc.ao2mo(mycc.mo_coeff)
        eris = mycc._eris
    
    cput1 = (logger.process_clock(), logger.perf_counter())

    # Use the existed amplitudes as initial guess
    if t1 is None: t1 = mycc.t1
    if t2 is None: t2 = mycc.t2
    if t1 is None and t2 is None:
        t1, t2 = mycc.get_init_guess(eris)
    elif t2 is None:
        t2 = mycc.get_init_guess(eris)[1]

    if comm.allreduce(getattr(mycc, "frozen_abab", False), op=mpi.MPI.LOR):
        nocc, nvir = t1.shape
        if nocc != nvir:
            log.warn("nocc (%s) != nvir (%s) for frozen_abab", nocc, nvir)
        mycc.remove_t2_abab(t2)
    if comm.allreduce(getattr(mycc, "frozen_aaaa_bbbb", False), op=mpi.MPI.LOR):
        nocc, nvir = t1.shape
        if nocc != nvir:
            log.warn("nocc (%s) != nvir (%s) for frozen_aaaa_bbbb", nocc, nvir)
        mycc.remove_t2_aaaa_bbbb(t2)
    if getattr(mycc, "t1_frozen_list", None) or getattr(mycc, "t2_frozen_list", None):
        nocc, nvir = t1.shape
        if nocc != nvir:
            log.warn("nocc (%s) != nvir (%s) for frozen amps", nocc, nvir)
        mycc.remove_amps(t1, t2, t1_frozen_list=mycc.t1_frozen_list,
                         t2_frozen_list=mycc.t2_frozen_list)

    eold = 0
    eccsd = mycc.energy(t1, t2, eris)
    log.info('Init E(CCSD) = %.15g', eccsd)
    
    mycc.t1 = t1
    mycc.t2 = t2
    vec = mycc.amplitudes_to_vector(t1, t2)
    mycc.vec = mycc.gather_vector(vec)
    # initialize the precond vector
    mycc.precond_vec = make_precond_vec_finv(mycc, t2, eris)
    if rank != 0:
        mycc.precond_vec = None
    mycc.cycle = 0
    return mycc
    
def make_precond_vec_finv(mycc, t2, eris, tol=1e-8):
    """
    Fock inversion as preconditioner.
    """
    nocc, _, nvir_seg, nvir = t2.shape
    ntasks = mpi.pool.size
    vlocs = [_task_location(nvir, task_id) for task_id in range(ntasks)]
    vloc0, vloc1 = vlocs[rank]
    
    mo_e_o = eris.mo_energy[:nocc]
    mo_e_v = eris.mo_energy[nocc:] + mycc.level_shift
    
    eia = mo_e_o[:, None] - mo_e_v
    eia[eia > -tol] = -tol
    t1Tnew = eia.T
    t2Tnew = lib.direct_sum('ia + jb -> abij', eia[:, vloc0:vloc1], eia)
    
    res = mycc.amplitudes_to_vector(t1Tnew.T, t2Tnew.transpose(2, 3, 0, 1))
    res = mycc.gather_vector(res)
    return res

@mpi.parallel_call(skip_args=[1], skip_kwargs=['vec'])
def distribute_vector_(mycc, vec=None, write='t'):
    """
    Distribute the entire vector of amplitudes tensor (nvec,) to
    different processes.
    will overwrite t1, t2 or l1, l2 according to write.
    """
    _sync_(mycc)
    sizes = comm.allgather(mycc.vector_size())
    # ZHC NOTE use gcd to avoid overflow of displacement.
    seg = reduce(math.gcd, sizes)
    sizes = np.cumsum(sizes)
    offsets = []
    for i in range(len(sizes)):
        if i == 0:
            offsets.append((0, sizes[i]))
        else:
            offsets.append((sizes[i-1], sizes[i]))

    if rank == 0:
        vec_segs = [vec[offset[0]:offset[1]].reshape(-1, seg) for offset in offsets]
        vec = mpi.scatter_new(vec_segs, data=vec)
    else:
        vec = mpi.scatter_new(None)
    vec = vec.ravel()
    if write == 't':
        mycc.t1, mycc.t2 = mycc.vector_to_amplitudes(vec)
    else:
        mycc.l1, mycc.l2 = mycc.vector_to_amplitudes(vec)
    return vec

@mpi.parallel_call
def gather_vector(mycc, vec=None):
    """
    Reconstruct the vector of amplitudes from the distributed vector.
    """
    sizes = comm.allgather(mycc.vector_size())
    seg = reduce(math.gcd, sizes)
    vec = mpi.gather_new(vec.reshape(-1, seg)).ravel()
    return vec

@mpi.parallel_call(skip_args=[1], skip_kwargs=['x'])
def get_res(mycc, x):
    """
    Get the residual vector of CC.

    Args:
        x: vector of CC amps (at root).
    Returns:
        res: vector of residual (at root).
    """
    _sync_(mycc)
    log = logger.new_logger(mycc, mycc.verbose)
    eris = getattr(mycc, '_eris', None)
    
    # firs distribute x to t1 and t2
    vec = mycc.distribute_vector_(x)
    t1, t2 = mycc.t1, mycc.t2

    eccsd = mycc.energy(t1, t2, eris)
    t1, t2 = mycc.update_amps(t1, t2, eris)

    # then gather the vector
    res = mycc.amplitudes_to_vector(t1, t2)
    norm = safe_max_abs(res)
    norm = comm.allreduce(norm, op=mpi.MPI.MAX)
    log.info("      cycle = %5d , E = %15.8g , norm(res) = %15.5g", mycc.cycle,
             eccsd, norm)
    mycc.cycle += 1

    res = mycc.gather_vector(res)
    return res

def mop(mycc, x):
    """
    preconditioner.

    Args:
        x: vector of CC amps (at root).
    Returns:
        res: x after applied precond.
    """
    #res = x / mycc.precond_vec
    #return res
    x /= mycc.precond_vec
    return x

def kernel(mycc):
    """
    Krylov kernel.
    """
    froot = opt.root
    tolnormt = mycc.conv_tol_normt
    max_cycle = mycc.max_cycle
    vec_size = mycc.vec.size

    if mycc.precond is None:
        M = None
    else:
        M = spla.LinearOperator((vec_size, vec_size), matvec=mycc.mop)
    
    x0 = mycc.vec

    if mycc.method == 'krylov':
        inner_m = mycc.inner_m
        outer_k = mycc.outer_k
        scipy_v = [int(sv) for sv in scipy.__version__.split(".")]
        if (scipy_v[0] < 1) or ((scipy_v[0] == 1) and (scipy_v[1] < 14)):
            # scipy version earlier than 1.14.0. Use inner_tol.
            jac_options = {'rdiff': 1e-6, 'inner_maxiter': 100,
                           'inner_inner_m': inner_m,
                           'inner_tol': tolnormt * 0.5,
                           'outer_k': outer_k, 'inner_M': M
                           }
        else:
            # inner_tol is deprecated from 1.14.0 onwards.
            jac_options = {'rdiff': 1e-6, 'inner_maxiter': 100,
                           'inner_inner_m': inner_m,
                           'inner_rtol': tolnormt * 0.5,
                           'outer_k': outer_k, 'inner_M': M
                           }
        res = froot(mycc.get_res, x0, method='krylov',
                    options={'fatol': tolnormt, 'tol_norm': safe_max_abs, 
                             'disp': True, 'maxiter': max_cycle // inner_m,
                             'line_search': 'wolfe',
                             'jac_options': jac_options
                            })
    else:
        raise ValueError

    conv = res.success
    mycc.distribute_vector_(res.x)
    eccsd = mycc.energy()
    mycc.e_corr = eccsd
    return conv, eccsd, mycc.t1, mycc.t2

def _init_ggccsd_krylov(ccsd_obj):
    from pyscf import gto
    from mpi4pyscf.tools import mpi
    from mpi4pyscf.cc import gccsd_krylov
    if mpi.rank == 0:
        mpi.comm.bcast((ccsd_obj.mol.dumps(), ccsd_obj.pack()))
    else:
        ccsd_obj = gccsd_krylov.GGCCSD_KRYLOV.__new__(gccsd_krylov.GGCCSD_KRYLOV)
        ccsd_obj.t1 = ccsd_obj.t2 = ccsd_obj.l1 = ccsd_obj.l2 = None
        mol, cc_attr = mpi.comm.bcast(None)
        ccsd_obj.mol = gto.mole.loads(mol)
        ccsd_obj.unpack_(cc_attr)

    if False:  # If also to initialize cc._scf object
        if mpi.rank == 0:
            if hasattr(ccsd_obj._scf, '_scf'):
                # ZHC FIXME a hack, newton need special treatment to broadcast
                e_tot = ccsd_obj._scf.e_tot
                ccsd_obj._scf = ccsd_obj._scf._scf
                ccsd_obj._scf.e_tot = e_tot
            mpi.comm.bcast((ccsd_obj._scf.__class__, _pack_scf(ccsd_obj._scf)))
        else:
            mf_cls, mf_attr = mpi.comm.bcast(None)
            ccsd_obj._scf = mf_cls(ccsd_obj.mol)
            ccsd_obj._scf.__dict__.update(mf_attr)

    key = id(ccsd_obj)
    mpi._registry[key] = ccsd_obj
    regs = mpi.comm.gather(key)
    return regs

class GGCCSD_KRYLOV(GGCCSD):
    """
    MPI GGCCSD using Newton-Krylov solver.
    """
    def __init__(self, mf, frozen=None, mo_coeff=None, mo_occ=None,
                 remove_h2=False, save_mem=False, 
                 diis_start_cycle=999999,
                 method='krylov', precond='finv', inner_m=10, outer_k=6,
                 frozen_abab=False, frozen_aaaa_bbbb=False, 
                 nocc_a=None, nvir_a=None,
                 t1_frozen_list=None, t2_frozen_list=None,
                 t1_fix_list=None, t2_fix_list=None):
        assert isinstance(mf, scf.ghf.GHF)
        gccsd.GCCSD.__init__(self, mf, frozen, mo_coeff, mo_occ)
        self.remove_h2 = remove_h2
        self.save_mem = save_mem
        self.rk = True
        self.diis_start_cycle = diis_start_cycle
        
        self.method = method
        self.precond = precond
        self.inner_m = inner_m
        self.outer_k = outer_k
        self.precond_vec = None
        
        self.frozen_abab = frozen_abab
        self.frozen_aaaa_bbbb = frozen_aaaa_bbbb
        self.nocc_a = nocc_a
        self.nvir_a = nvir_a
        self.t1_frozen_list = t1_frozen_list
        self.t2_frozen_list = t2_frozen_list
        self.t1_fix_list = t1_fix_list
        self.t2_fix_list = t2_fix_list

        self._keys = self._keys.union(["remove_h2", "save_mem", "rk", 
                                       "method", "precond", "inner_m", "outer_k",
                                       "precond_vec", "frozen_abab", "frozen_aaaa_bbbb", 
                                       "nocc_a", "nvir_a", 
                                       "t1_frozen_list", "t2_frozen_list",
                                       "t1_fix_list", "t2_fix_list"])

        regs = mpi.pool.apply(_init_ggccsd_krylov, (self,), (None,))
        self._reg_procs = regs
    
    def dump_flags(self, verbose=None):
        if rank == 0:
            GGCCSD.dump_flags(self, verbose)
            logger.info(self, "method  = %s", self.method)
            logger.info(self, "precond = %s", self.precond)
            logger.info(self, "inner_m = %d", self.inner_m)
            logger.info(self, "outer_k = %d", self.outer_k)
            logger.info(self, "frozen_abab = %s", self.frozen_abab)
            logger.info(self, "frozen_aaaa_bbbb = %s", self.frozen_aaaa_bbbb)
            logger.info(self, "nocc_a  = %s", self.nocc_a)
            logger.info(self, "nvir_a  = %s", self.nvir_a)
            logger.info(self, "t1_frozen_list  = %s", self.t1_frozen_list)
            logger.info(self, "t2_frozen_list  = %s", self.t2_frozen_list)
            logger.info(self, "t1_fix_list  = %s", self.t1_fix_list)
            logger.info(self, "t2_fix_list  = %s", self.t2_fix_list)
        return self
    
    def pack(self):
        return {'verbose'    : self.verbose,
                'max_memory' : self.max_memory,
                'frozen'     : self.frozen,
                'mo_coeff'   : self.mo_coeff,
                'mo_occ'     : self.mo_occ,
                '_nocc'      : self._nocc,
                '_nmo'       : self._nmo,
                'diis_file'  : self.diis_file,
                'level_shift': self.level_shift,
                'direct'     : self.direct,
                'diis_space' : self.diis_space,
                'diis_start_cycle' : self.diis_start_cycle,
                'remove_h2'  : self.remove_h2,
                'save_mem'   : self.save_mem,
                'rk'         : self.rk,
                'method'     : self.method,
                'precond'    : self.precond,
                'inner_m'    : self.inner_m,
                'outer_k'    : self.outer_k,
                'frozen_abab': self.frozen_abab,
                'frozen_aaaa_bbbb': self.frozen_aaaa_bbbb,
                'nocc_a'     : self.nocc_a,
                'nvir_a'     : self.nvir_a,
                't1_frozen_list': self.t1_frozen_list,
                't2_frozen_list': self.t2_frozen_list,
                't1_fix_list': self.t1_fix_list,
                't2_fix_list': self.t2_fix_list,
                }
    
    def ccsd(self, t1=None, t2=None, eris=None):
        assert self.mo_coeff is not None
        assert self.mo_occ is not None
        if self.verbose >= logger.WARN:
            self.check_sanity()
        self.dump_flags()
        
        if self.e_hf is None:
            self.e_hf = self._scf.e_tot
        
        pre_kernel(self, eris, t1, t2, max_cycle=self.max_cycle,
                   tol=self.conv_tol, tolnormt=self.conv_tol_normt,
                   verbose=self.verbose)
        
        self.converged, self.eccsd, self.t1, self.t2 = kernel(self)
        
        if rank == 0:
            self._finalize()
        return self.e_corr, self.t1, self.t2
    
    def solve_lambda(self, t1=None, t2=None, l1=None, l2=None,
                     eris=None, approx_l=False):
        from mpi4pyscf.cc import gccsd_lambda_krylov
        gccsd_lambda_krylov.pre_kernel(self, eris, t1, t2, l1, l2,
                                       max_cycle=self.max_cycle,
                                       tol=self.conv_tol_normt,
                                       verbose=self.verbose, approx_l=approx_l)
        
        if approx_l:
            conv = True
        else:
            conv, self.l1, self.l2 = gccsd_lambda_krylov.kernel(self)
        self.converged_lambda = conv
        return self.l1, self.l2
    
    mop = mop
    distribute_vector_ = distribute_vector_
    gather_vector = gather_vector
    get_res = get_res
    
    remove_t2_abab = remove_t2_abab
    remove_t2_aaaa_bbbb = remove_t2_aaaa_bbbb
    remove_amps = remove_amps
    set_zero_amps = set_zero_amps
    analyze_amps = analyze_amps

if __name__ == '__main__':
    from pyscf import gto
    import time
    
    np.random.seed(1)
    np.set_printoptions(4, linewidth=1000, suppress=True)
    
    # genrate reference values
    mol = gto.M(
        atom = 'H 0 0 0; F 0 0 1.1',
        basis = '321g',
        spin = 2)
    mol.verbose = 5
    mf = mol.GHF()
    hcoreX = mf.get_hcore()
    # a small random potential to break the Sz symmetry:
    pot = (np.random.random(hcoreX.shape) - 0.5) * 3e-2
    pot = pot + pot.T
    hcoreX += pot
    mf.get_hcore = lambda *args: hcoreX
    mf.kernel()

    mycc_ref = gccsd.GCCSD(mf)
    mycc_ref.conv_tol = 1e-10
    mycc_ref.conv_tol_normt = 1e-6
    eris_ref = mycc_ref.ao2mo()

    # converged Ecc
    ecc_ref, t1_cc_ref, t2_cc_ref = mycc_ref.kernel()

    # test class
    mycc = GCCSD(mf)
    mycc.conv_tol = 1e-10
    mycc.conv_tol_normt = 1e-6

    print ("test CC converged energy and t1, t2")
    
    e_cc, t1_cc, t2_cc = mycc.kernel()
    print (abs(e_cc - ecc_ref))
    assert abs(e_cc - ecc_ref) < 1e-8
    
    t1_cc, t2_cc = mycc.gather_amplitudes()
    t1_diff = la.norm(t1_cc - t1_cc_ref)
    print (t1_diff)
    assert t1_diff < 1e-7

    if rank == 0:
        t2_diff = la.norm(t2_cc - t2_cc_ref)
        print (t2_diff)
        assert t2_diff < 1e-7

