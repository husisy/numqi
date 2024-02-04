from ._internal import PositiveReal, to_positive_real_exp, to_positive_real_softplus
from ._internal import OpenInterval, to_open_interval
from ._internal import Ball, to_ball
from ._internal import Trace1PSD, to_trace1_psd_cholesky, to_trace1_psd_ensemble
from ._internal import SymmetricMatrix, to_symmetric_matrix
from ._internal import Sphere, to_sphere_quotient, to_sphere_coordinate
from ._internal import DiscreteProbability, to_discrete_probability_sphere, to_discrete_probability_softmax
from ._internal import Stiefel, to_stiefel_choleskyL, to_stiefel_qr, to_stiefel_sqrtm
from ._internal import SpecialOrthogonal, to_special_orthogonal_exp, to_special_orthogonal_cayley

# composed
from ._compose import quantum_state, density_matrix, SeparableDensityMatrix, QuantumChannel

from ._misc import StiefelManifoldDistanceModel, GrassmannManifoldDistanceModel, TwoHermitianSumModel

from . import _internal
from . import _misc
from . import _compose

# api change
'''
## old behavior
# used in numpy
psi = numqi.random.rand_sphere()
psi = numqi.param.real_to_sphere(np0)
# used in pytorch
theta = torch.nn.Parameter(xxx)
psi = numqi.param.real_to_sphere(theta)


## new behavior
# used in numpy
psi = numqi.manifold.to_sphere(np0)
psi = numqi.random.rand_sphere(dim, batch_size=None, dtype='float64')

# used in pytorch
psi = numqi.manifold.to_sphere(torch.Tensor)
manifold = numqi.manifold.Sphere(23, dtype=torch.complex128)
psi = manifold()
'''