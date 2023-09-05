import torch
import numpy as np
import cvxpy
from tqdm import tqdm
import contextlib
import scipy.linalg
import math

import numqi.gellmann
import numqi.random
import numqi.utils
import numqi.optimize

from ._misc import get_density_matrix_boundary, hf_interpolate_dm, _ree_bisection_solve

def _rand_norm_bounded_unitary(dim, norm2_bound, N0, np_rng):
    # |A|_2 <= |A|_F
    # https://math.stackexchange.com/a/252831
    tmp0 = np_rng.normal(size=(N0,dim,dim)) + 1j*np_rng.normal(size=(N0,dim,dim))
    ret = tmp0 + tmp0.transpose(0,2,1).conj()
    norm = np.linalg.norm(ret, axis=(1,2), ord='fro', keepdims=True)
    ret = scipy.linalg.expm(ret*(1j*norm2_bound/norm))
    return ret


def _cha_reset_state(ketA, ketB, probability, threshold, norm, np_rng, indexR=None):
    mask_low_prob = probability<threshold
    mask_drop = mask_low_prob if (indexR is None) else np.logical_and(mask_low_prob, indexR)
    num_drop = mask_drop.sum()
    num_state,dimA = ketA.shape
    dimB = ketB.shape[1]
    assert ketB.shape[0]==num_state
    if num_drop:
        ind_keep = np.nonzero(np.logical_not(mask_low_prob))[0]
        # TODO choice according to the probability
        ind_select = np_rng.choice(ind_keep, size=num_drop, replace=True)
        tmp0 = _rand_norm_bounded_unitary(dimA, norm, num_drop, np_rng)
        ketA_new = np.einsum(tmp0, [0,1,2], ketA[ind_select], [0,2], [0,1], optimize=True)
        tmp0 = _rand_norm_bounded_unitary(dimB, norm, num_drop, np_rng)
        ketB_new = np.einsum(tmp0, [0,1,2], ketB[ind_select], [0,2], [0,1], optimize=True)
        ret = mask_drop,ketA_new,ketB_new
    else:
        ret = None,None,None
    return ret


class CHABoundaryBagging:
    # CHA with bagging 10.1103/PhysRevA.98.012315
    def __init__(self, dim, num_state=None) -> None:
        assert len(dim)==2
        dimA,dimB = dim
        num_state = 3*(dimA*dimB)**2 if (num_state is None) else num_state
        self.dimA = dim[0]
        self.dimB = dim[1]
        # 3*dimA*dimB*dimA*dimB looks good for 3x3 bipartite system
        self.num_state = num_state

        self.cvx_beta = cvxpy.Variable(name='beta')
        self.cvx_lambda = cvxpy.Variable(num_state, name='lambda')
        self.cvx_A_r = cvxpy.Parameter((num_state,dimA*dimB*dimA*dimB))
        self.cvx_A_i = cvxpy.Parameter((num_state,dimA*dimB*dimA*dimB))
        self.cvx_obj = cvxpy.Maximize(self.cvx_beta)
        self.cvx_problem = None
        self.dm_target = None
        self.ketA = None
        self.ketB = None

    def _rand_init_state(self, np_rng, max_retry):
        assert max_retry>0
        def hf0(sz0, sz1):
            ret = np_rng.normal(size=(sz0, sz1*2)).astype(np.float64, copy=False).view(np.complex128)
            ret /= np.linalg.norm(ret, axis=1, keepdims=True)
            return ret
        for _ in range(max_retry):
            self.ketA = hf0(self.num_state, self.dimA)
            self.ketB = hf0(self.num_state, self.dimB)
            beta = self._cvxpy_solve()
            if (beta is not None) and (not math.isinf(beta)):
                break
        else:
            raise RuntimeError('Failed to find a good initial state')
        ind0 = np.argsort(self.cvx_lambda.value)[::-1]
        self.ketA = self.ketA[ind0]
        self.ketB = self.ketB[ind0]

    def _cvxpy_solve(self):
        N0 = self.dimA*self.dimB
        tmp0 = np.einsum(self.ketA,[0,1],self.ketA.conj(),[0,3],self.ketB,[0,2],self.ketB.conj(),[0,4],[0,1,2,3,4],optimize=True)
        tmp0 = (tmp0.reshape(-1,N0,N0) - np.eye(N0)/N0).reshape(-1,N0*N0)
        self.cvx_A_r.value = tmp0.real
        self.cvx_A_i.value = tmp0.imag
        ret = self.cvx_problem.solve(ignore_dpp=True) #ECOS solver
        # ret and self.cvx_lambda.value could be None if num_state is too small
        return ret

    def set_dm_target(self, dm):
        N0 = self.dimA*self.dimB
        assert dm.shape==(N0,N0)
        assert abs(np.trace(dm)-1) < 1e-10
        assert np.abs(dm-dm.T.conj()).max() < 1e-10
        self.dm_target = dm.copy()
        dm_normed = (dm - np.eye(N0)/N0).reshape(-1) / numqi.gellmann.dm_to_gellmann_norm(dm)
        cvx_constrants = [
            self.cvx_beta*dm_normed.real==self.cvx_lambda @ self.cvx_A_r,
            self.cvx_beta*dm_normed.imag==self.cvx_lambda @ self.cvx_A_i,
            self.cvx_lambda>=0,
            cvxpy.sum(self.cvx_lambda)==1,
        ]
        self.cvx_problem = cvxpy.Problem(self.cvx_obj, cvx_constrants)

    def solve(self, dm, maxiter=150, norm2_init=1, decay_rate=0.97, threshold=1e-7,
                num_init_retry=10, use_tqdm=False, return_info=False, seed=None):
        self.set_dm_target(dm)
        np_rng = numqi.random.get_numpy_rng(seed)
        if num_init_retry>0:
            self._rand_init_state(np_rng, num_init_retry)
        beta_history = [self._cvxpy_solve()]
        assert beta_history[-1] is not None, 'cvxpy solve failed, num_state might be too small'
        norm2_bound = norm2_init
        with (tqdm(range(maxiter)) if use_tqdm else contextlib.nullcontext()) as pbar:
            for _ in (pbar if use_tqdm else range(maxiter)):
                if use_tqdm:
                    pbar.set_postfix_str(f'beta={beta_history[-1]:.5f}, eps={norm2_bound:.4f}')
                mask,tmp2,tmp3 = _cha_reset_state(self.ketA, self.ketB, self.cvx_lambda.value, threshold, norm2_bound, np_rng)
                if mask is not None:
                    self.ketA[mask] = tmp2
                    self.ketB[mask] = tmp3
                norm2_bound *= decay_rate
                beta_history.append(self._cvxpy_solve())
        beta = beta_history[-1]
        if return_info:
            mask = self.cvx_lambda.value > 0
            ret = beta, (self.ketA[mask],self.ketB[mask],self.cvx_lambda.value[mask], beta_history)
        else:
            ret = beta
        return ret


class AutodiffCHAREE(torch.nn.Module):
    def __init__(self, dim0, dim1, num_state=None, distance_kind='ree'):
        super().__init__()
        # [2*dA*dB,3*dA*dB] seems to be good enough
        num_state = (2*dim0*dim1) if (num_state is None) else num_state
        distance_kind = distance_kind.lower()
        assert distance_kind in {'gellmann', 'ree'}
        self.distance_kind = distance_kind
        self.num_state = num_state
        self.dim0 = dim0
        self.dim1 = dim1

        np_rng = np.random.default_rng()
        hf0 = lambda *size: torch.nn.Parameter(torch.tensor(np_rng.uniform(-1,1,size=size), dtype=torch.float64, requires_grad=True))
        self.theta_p = hf0(num_state)
        self.theta_psi0 = hf0(2, num_state, dim0) #2 for real and imag
        self.theta_psi1 = hf0(2, num_state, dim1)

        self.dm_sep_torch = None
        self.probability = None

        self.dm_target = None
        self.expect_op_T_vec = None

    def set_dm_target(self, dm):
        assert dm.shape[0]==(self.theta_psi0.shape[2]*self.theta_psi1.shape[2])
        assert dm.shape[0]==dm.shape[1]
        self.dm_target = torch.tensor(dm, dtype=torch.complex128)

    def set_expectation_op(self, op):
        self.dm_target = None
        self.expect_op_T_vec = torch.tensor(op.T.reshape(-1), dtype=torch.complex128)


    def forward(self):
        probability = torch.nn.functional.softmax(self.theta_p, dim=0)
        # self.np_rng = numqi.random.get_numpy_rng(seed)
        # self.reset_threshold = 1e-4
        # self.reset_round_max = 10
        # self.reset_norm = 1e-1
        # self.reset_round = np.zeros(num_state, dtype=np.int64)
        # if not tag_fix:
        #     tmp0 = torch.complex(self.theta_psi0[0], self.theta_psi0[1]).detach().numpy()
        #     tmp1 = torch.complex(self.theta_psi1[0], self.theta_psi1[1]).detach().numpy()
        #     ind0,tmp2,tmp3 = _cha_reset_state(tmp0, tmp1, probability.detach().numpy(),
        #                 self.reset_threshold, self.reset_round<=0, self.reset_norm, self.np_rng)
        #     self.reset_round = np.maximum(self.reset_round-1, 0)
        #     if ind0 is not None:
        #         # in most cases, these code will not be executed
        #         self.theta_psi0.data[0,ind0] = torch.tensor(tmp2.real, dtype=self.theta_psi0.dtype)
        #         self.theta_psi0.data[1,ind0] = torch.tensor(tmp2.imag, dtype=self.theta_psi0.dtype)
        #         self.theta_psi1.data[0,ind0] = torch.tensor(tmp3.real, dtype=self.theta_psi1.dtype)
        #         self.theta_psi1.data[1,ind0] = torch.tensor(tmp3.imag, dtype=self.theta_psi1.dtype)
        #         self.reset_round[ind0] = self.reset_round_max
        tmp0 = torch.complex(self.theta_psi0[0], self.theta_psi0[1])
        state0 = tmp0 / torch.linalg.norm(tmp0, dim=1, keepdim=True)
        tmp0 = torch.complex(self.theta_psi1[0], self.theta_psi1[1])
        state1 = tmp0 / torch.linalg.norm(tmp0, dim=1, keepdim=True)
        dm_sep_torch = torch.einsum(probability, [0], state0, [0,1], state0.conj(), [0,3], state1, [0,2], state1.conj(), [0,4], [1,2,3,4]).reshape(self.dim0*self.dim1,-1)
        self.probability = probability
        self.dm_sep_torch = dm_sep_torch.detach()
        if self.dm_target is not None:
            if self.distance_kind=='gellmann':
                tmp0 = (self.dm_target - dm_sep_torch).reshape(-1)
                loss = 2 * torch.dot(tmp0, tmp0.conj()).real
                # the 2 is because Tr[Mi*Mj] = 2 delta_ij
            else:
                loss = numqi.utils.get_relative_entropy(self.dm_target, dm_sep_torch)
        else:
            loss = torch.dot(dm_sep_torch.reshape(-1), self.expect_op_T_vec).real
        return loss

    def get_boundary(self, dm0, xtol=1e-4, converge_tol=1e-10, threshold=1e-7, num_repeat=1, use_tqdm=True, return_info=False, seed=None):
        beta_u = get_density_matrix_boundary(dm0)[1]
        dm0_norm = numqi.gellmann.dm_to_gellmann_norm(dm0)
        np_rng = numqi.random.get_numpy_rng(seed)
        def hf0(beta):
            # use alpha to avoid time-consuming gellmann conversion
            tmp0 = hf_interpolate_dm(dm0, alpha=beta/dm0_norm)
            self.set_dm_target(tmp0)
            theta_optim = numqi.optimize.minimize(self, theta0='uniform',
                        tol=converge_tol, num_repeat=num_repeat, seed=np_rng, print_every_round=0)
            return float(theta_optim.fun)
        beta,history_info = _ree_bisection_solve(hf0, 0, beta_u, xtol, threshold, use_tqdm=use_tqdm)
        ret = (beta,history_info) if return_info else beta
        return ret

    def get_numerical_range(self, op0, op1, num_theta=400, converge_tol=1e-5, num_repeat=1, use_tqdm=True, seed=None):
        np_rng = numqi.random.get_numpy_rng(seed)
        N0 = self.dim0*self.dim1
        assert (op0.shape==(N0,N0)) and (op1.shape==(N0,N0))
        theta_list = np.linspace(0, 2*np.pi, num_theta)
        ret = []
        kwargs = dict(num_repeat=num_repeat, seed=np_rng, print_every_round=0, tol=converge_tol)
        for theta_i in (tqdm(theta_list) if use_tqdm else theta_list):
            # see pyqet.entangle.ppt.get_ppt_numerical_range, we use the maximization there
            self.set_expectation_op(-np.cos(theta_i)*op0 - np.sin(theta_i)*op1)
            numqi.optimize.minimize(self, **kwargs)
            rho = self.dm_sep_torch.numpy()
            ret.append([np.trace(x @ rho).real for x in [op0,op1]])
        ret = np.array(ret)
        return ret


class CHABoundaryAutodiff(torch.nn.Module):
    # combine convex optimization with gradient descent
    # TODO _rand_init_state
    def __init__(self, dimA, dimB, num_state=None, seed=None):
        print('[WARNING] CHABoundaryAutodiff bad performance, use "AutodiffCHAREE.get_boundary" or "CHABoundaryBagging" instead')
        super().__init__()
        num_state = 3*(dimA*dimB)**2 if (num_state is None) else num_state
        self.num_state = num_state
        self.dimA = dimA
        self.dimB = dimB

        np_rng = np.random.default_rng(seed)
        hf0 = lambda *size: torch.nn.Parameter(torch.tensor(np_rng.uniform(-1,1,size=size), dtype=torch.float64, requires_grad=True))
        self.theta_psiA = hf0(2, num_state, dimA) #2 for real and imag
        self.theta_psiB = hf0(2, num_state, dimB)
        self.np_rng = np_rng

        self.solver_args = dict()
        # self.solver_args = dict(eps=solver_eps, solve_method='ECS') #ECOS SCS MOSEK

        # set in .set_dm_target()
        self.dm_target = None
        self.cvxpylayer = None
        self.lambda_value = None

        self.reset_round_max = 10
        self.reset_norm = 1e-1
        self.reset_threshold = 1e-7
        self.reset_round = np.zeros(num_state, dtype=np.int64)

    def set_dm_target(self, rho):
        dimA = self.dimA
        dimB = self.dimB
        num_state = int(self.theta_psiA.shape[1])
        assert rho.shape==(dimA*dimB,dimA*dimB)
        assert np.abs(rho-rho.T.conj()).max() < 1e-10
        assert abs(np.trace(rho)-1) < 1e-10
        # rho cannot be maximally mixed state
        rho_norm = numqi.gellmann.dm_to_gellmann_norm(rho)
        assert rho_norm > 1e-10
        self.dm_target = rho.copy()

        import cvxpylayers.torch
        tmp0 = dimA*dimA*dimB*dimB
        tmp1 = (rho - np.eye(dimA*dimB)/(dimA*dimB)).reshape(-1)/rho_norm
        # tmp1 = np.eye(dimA*dimB, dtype=np.complex128).view(np.float64).reshape(-1)/(dimA*dimB)
        # tmp2 = np.asarray(rho, dtype=np.complex128).view(np.float64).reshape(-1)
        cvx_beta = cvxpy.Variable(name='beta', complex=False)
        cvx_lambda = cvxpy.Variable(num_state, name='lambda', complex=False)
        # cvxpylayers not support complex
        cvx_rho_r = cvxpy.Parameter((num_state,tmp0))
        cvx_rho_i = cvxpy.Parameter((num_state,tmp0))
        cvx_obj = cvxpy.Maximize(cvx_beta)
        cvx_constrants = [
            cvx_beta*(tmp1.real)==cvx_lambda@cvx_rho_r,
            cvx_beta*(tmp1.imag)==cvx_lambda@cvx_rho_i,
            cvx_lambda>=0,
            cvxpy.sum(cvx_lambda)==1,
        ]
        cvx_problem = cvxpy.Problem(cvx_obj, cvx_constrants)
        self.cvxpylayer = cvxpylayers.torch.CvxpyLayer(cvx_problem, parameters=[cvx_rho_r,cvx_rho_i], variables=[cvx_beta,cvx_lambda])

    def _hf0(self, tag_fix):
        dim = self.dimA*self.dimB
        assert self.cvxpylayer is not None
        tmp0 = torch.complex(self.theta_psiA[0], self.theta_psiA[1])
        stateA = tmp0 / torch.linalg.norm(tmp0, dim=1, keepdim=True)
        tmp0 = torch.complex(self.theta_psiB[0], self.theta_psiB[1])
        stateB = tmp0 / torch.linalg.norm(tmp0, dim=1, keepdim=True)
        tmp0 = torch.einsum(stateA, [0,1], stateA.conj(), [0,3], stateB, [0,2], stateB.conj(), [0,4], [0,1,2,3,4])
        tmp1 = (tmp0.reshape(-1,dim,dim) - torch.eye(dim)/dim).reshape(self.num_state,dim*dim)
        # tmp1 = torch.stack([tmp0.real, tmp0.imag], dim=-1).reshape(self.num_state,-1)
        beta_,lambda_ = self.cvxpylayer(tmp1.real, tmp1.imag, solver_args=self.solver_args)
        if not tag_fix:
            tmp0 = torch.complex(self.theta_psiA[0], self.theta_psiA[1]).detach().numpy()
            tmp1 = torch.complex(self.theta_psiB[0], self.theta_psiB[1]).detach().numpy()
            ind0,tmp2,tmp3 = _cha_reset_state(tmp0, tmp1, lambda_.detach().numpy(),
                        self.reset_threshold, self.reset_norm, self.np_rng, self.reset_round<=0)
            self.reset_round = np.maximum(self.reset_round-1, 0)
            if ind0 is not None:
                self.theta_psiA.data[0,ind0] = torch.tensor(tmp2.real, dtype=self.theta_psiA.dtype)
                self.theta_psiA.data[1,ind0] = torch.tensor(tmp2.imag, dtype=self.theta_psiA.dtype)
                self.theta_psiB.data[0,ind0] = torch.tensor(tmp3.real, dtype=self.theta_psiB.dtype)
                self.theta_psiB.data[1,ind0] = torch.tensor(tmp3.imag, dtype=self.theta_psiB.dtype)
                self.reset_round[ind0] = self.reset_round_max
        return beta_.real,lambda_.real,stateA,stateB

    def forward(self, tag_fix=False):
        loss = -self._hf0(tag_fix)[0]
        return loss

    def get_state(self):
        with torch.no_grad():
            alpha_,lambda_,stateA,stateB = self._hf0(tag_fix=True)
            alpha_ = alpha_.item()
            lambda_ = lambda_.detach().numpy()
            stateA = stateA.detach().numpy()
            stateB = stateB.detach().numpy()
        return alpha_,lambda_,stateA,stateB