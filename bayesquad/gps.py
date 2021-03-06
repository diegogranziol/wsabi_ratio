"""Provides classes for Gaussian Process models, including models where a warping of the output space has been applied.
"""

from abc import ABC, abstractmethod
from typing import Tuple, Union

import GPy.core.gp
import numpy as np
from numpy import ndarray

from . import kernel_gradients
from ._util import validate_dimensions
from ._cache import last_value_cache, clear_last_value_caches
from .decorators import flexible_array_dimensions
from .maths_helpers import jacobian_of_f_squared_times_g, hessian_of_f_squared_times_g


class GP:
    """Wrapper around a GPy GP, providing some convenience methods and gradient calculations.

    All methods and properties of a GPy GP may be accessed directly on an instance of this class, and will be passed
    through to the wrapped GPy GP instance.

    Warnings
    --------
    The following methods of this class cache their return value for the most recently passed argument:
        - :func:`~posterior_mean_and_variance`
        - :func:`~posterior_jacobians`
        - :func:`~posterior_hessians`

    This is a performance optimisation to prevent duplication of work (e.g. a :class:`~WarpedGP` may need to call
    posterior_mean_and_variance to compute its own posterior mean, and then immediately do so again to compute its
    posterior jacobians). The cache is cleared whenever the underlying GP is modified (this is implemented using the
    observer mechanism provided by GPy). This should mean that a cache hit will only occur if the result of performing
    the computation again would be exactly the same, but if necessary (e.g. if `update_model` has been disabled on the
    underlying GPy `GP`), it is possible to clear the cache manually by calling the method :func:`_clear_cache` on an
    instance of this class.

    Note that the cache is not shared between instances - each instance of this class will have its own separate cache.

    See Also
    --------
    :class:`GPy.core.gp.GP`
    """
    def __init__(self, gpy_gp: GPy.core.gp.GP):
        self._gpy_gp = gpy_gp
        self.dimensions = gpy_gp.input_dim

        gpy_gp.add_observer(self, self._clear_cache)

    def __getattr__(self, item):
        # Given a property foo which is defined on the GPy GP class, but not on this class, this method ensures that
        # accessing self.foo will return self._gpy_gp.foo. Similarly, if some other code has gp = GP(), then gp.foo will
        # return gp._gpy_gp.foo.
        return getattr(self._gpy_gp, item)

    def __del__(self):
        self._clear_cache()

    @last_value_cache
    @flexible_array_dimensions
    def posterior_mean_and_variance(self, x: ndarray, *args, **kwargs) -> Tuple[ndarray, ndarray]:
        """Get the posterior mean and variance at a point, or a set of points.

        Parameters
        ----------
        x
            The point(s) at which to evaluate the posterior mean and variance. A 2D array of shape
            (num_points, num_dimensions), or a 1D array of shape (num_dimensions).

        Returns
        -------
        mean : ndarray
            A 1D array of shape (num_points) if the input was 2D, or a 0D array if the input was 1D. The :math:`i`-th
            element is the posterior mean at the :math:`i`-th point of `x`.
        variance : ndarray
            A 1D array of shape (num_points) if the input was 2D, or a 0D array if the input was 1D. The :math:`i`-th
            element is the posterior variance at the :math:`i`-th point of `x`.

        See Also
        --------
        GPy.core.gp.GP.predict : This method wraps GPy.core.gp.GP.predict, and will pass through any further positional
            or keyword arguments.
        """
        validate_dimensions(x, self.dimensions)
        mean, variance = self._gpy_gp.predict(x, *args, **kwargs)

        return np.squeeze(mean, axis=-1), np.squeeze(variance, axis=-1)

    @last_value_cache
    @flexible_array_dimensions
    def posterior_jacobians(self, x: ndarray, *args, **kwargs) -> Tuple[ndarray, ndarray]:
        """Get the jacobian of the posterior mean and the jacobian of the posterior variance.

        Parameters
        ----------
        x
            The point(s) at which to evaluate the posterior jacobians. A 2D array of shape (num_points, num_dimensions),
            or a 1D array of shape (num_dimensions).

        Returns
        -------
        mean_jacobian : ndarray
            An array of the same shape as the input. The :math:`(i, j)`-th element is the :math:`j`-th component of the
            jacobian of the posterior mean at the :math:`i`-th point of `x`.
        variance_jacobian : ndarray
            An array of the same shape as the input. The :math:`(i, j)`-th element is the :math:`j`-th component of the
            jacobian of the posterior variance at the :math:`i`-th point of `x`.

        See Also
        --------
        GPy.core.gp.GP.predictive_gradients : This method wraps GPy.core.gp.GP.predictive_gradients, and will pass
            through any additional positional or keyword arguments.
        """
        validate_dimensions(x, self.dimensions)
        mean_jacobian, variance_jacobian = self._gpy_gp.predictive_gradients(x, *args, **kwargs)

        return np.squeeze(mean_jacobian, axis=-1), variance_jacobian

    @last_value_cache
    @flexible_array_dimensions
    def posterior_hessians(self, x: ndarray) -> Tuple[ndarray, ndarray]:
        """Get the hessian of the posterior mean and the hessian of the posterior variance.

        Given a set of points, return the hessian of the posterior mean and the hessian of the posterior variance at
        each point.

        Parameters
        ----------
        x
            A 2D array of shape (num_points, num_dimensions), or a 1D array of shape (num_dimensions).

        Returns
        -------
        mean_hessian : ndarray
            A 3D array of shape (num_points, num_dimensions, num_dimensions) if the input was 2D, or a 2D array of shape
            (num_dimensions, num_dimensions) if the input was 1D. The :math:`(i,j,k)`-th element is the :math:`(j,k)`-th
            mixed partial derivative of the posterior mean at the :math:`i`-th point of `x`.
        variance_hessian : ndarray
            A 3D array of shape (num_points, num_dimensions, num_dimensions) if the input was 2D, or a 2D array of shape
            (num_dimensions, num_dimensions) if the input was 1D. The :math:`(i,j,k)`-th element is the :math:`(j,k)`-th
            mixed partial derivative of the posterior variance at the :math:`i`-th point of `x`.

        Notes
        -----
        This code deals with up to 4-dimensional tensors and getting all the dimensions lined up correctly is slightly
        painful.

        In the following:

            - :math:`X_*` is the set of points at which to evaluate the hessians (i.e. the input to this method). In the
              code, this is `x`.
            - :math:`D = \{ X_D, Y_D \}` is our GP's data (with :math:`X_D` the locations of function evaluations, and
              :math:`Y_D` the values of the function evaluations). In the code, these are `X_D` and `Y_D`
            - :math:`n` is the number of points in :math:`X_*`.
            - :math:`N` is the number of points in :math:`X_D`.
            - :math:`d` is the number of dimensions.
            - :math:`K` is the kernel of our GP. In the code, this is `self.kern.K`.
            - :math:`K_D` is the matrix with elements :math:`(K_D)_{ij} = K(x_i, x_j)` for :math:`x_i, x_j \in X_D`. In
              the code, :math:`K_D^{-1}` is `K_D_inv`.
            - :math:`K_*` is the :math:`n` by :math:`N` matrix with elements :math:`(K_*)_{ij} = K(x_i, x_j)`
              for :math:`x_i \in X_*, x_j \in X_D`. In the code, this is `K_star`.
            - :math:`m(X_*)` is the posterior mean at :math:`X_*`, which is a vector of length :math:`n`.
            - :math:`V(X_*)` is the posterior variance at :math:`X_*`, which is a vector of length :math:`n`.

        The hessians we return depend on the jacobian and hessian of :math:`K_*`. Since :math:`K_*` is a matrix, the
        jacobian is a 3D tensor, and the hessian is a 4D tensor. Writing :math:`J` for the jacobian and :math:`H` for
        the hessian, we have:

        .. math::

            J_{ijk}  & = & \\frac{\\partial (K_*)_{ij}}{\\partial x_k} \\\\
                     & = & \\frac{\\partial K((X_*)_i, (X_D)_j)}{\\partial x_k}

            H_{ijkl} & = & \\frac{\\partial^2 (K_*)_{ij}}{\\partial x_k \\partial x_l} \\\\
                     & = & \\frac{\\partial^2 K((X_*)_i, (X_D)_j)}{\\partial x_k \\partial x_l} \\\\

        In the code, :math:`J` is `kernel_jacobian`, and :math:`H` is `kernel_hessian`. These have shape
        (:math:`n, N, d`) and (:math:`n, N, d, d`) respectively.

        The hessian of the mean is reasonably straightforward. We have:

        .. math::

            m(X_*)   & = & K_* K_D^{-1} Y_D \\\\
            m(X_*)_i & = & (K_*)_{ij} (K_D^{-1})_{jk} (Y_D)_k \\\\
            \\frac{\\partial^2 m(X_*)_i}{\\partial x_k \\partial x_l}
                     & = &
            H_{ijkl} (K_D^{-1})_{jm} (Y_D)_m \\\\

        The hessian of the variance is more complicated. It is the difference of a data-independent diagonal part,
        :math:`P`, and a data-dependent part, :math:`Q`, as follows:

        .. math::

            V(X_*)_i  & = & K((X_*)_i, (X_*)_i) - (K_*)_{ij} (K_D^{-1})_{jk} (K_*)_{ik} \\\\
            \\frac{\\partial^2 V(X_*)_i}{\\partial x_j \\partial x_k} & = & P_{ijk} - Q_{ijk} \\\\
            P_{ijk}  & = & \\frac{\\partial^2 K((X_*)_i, (X_*)_i)}{\\partial x_j \\partial x_k} \\\\
            Q_{ijk}  & = & \\hat{Q}_{ijk} + \\hat{Q}_{ikj} \\\\
            \\hat{Q}_{ijk} & = & \\frac{\\partial^2 (K_*)_{il}}{\\partial x_j \\partial x_k} (K_D^{-1})_{lm} (K_*)_im
            + \\frac{\\partial (K_*)_{il}}{\\partial x_j}(K_D^{-1})_{lm}\\frac{\\partial (K_*)_{im}}{\\partial x_k} \\\\
                           & = & H_{iljk} (K_D^{-1})_{lm} (K_*)_m + J_{ilj} (K_D^{-1})_{lm} J_{imk} \\\\

        In the code, :math:`P` and :math:`Q` are `diagonal_hessian` and `data_dependent_hessian`, respectively.
        """
        validate_dimensions(x, self.dimensions)
        kernel_jacobian = self._kernel_jacobian(x)
        kernel_hessian = self._kernel_hessian(x)

        X_D = self.X
        Y_D = np.atleast_1d(np.squeeze(self.Y))

        K_D_inv = self.posterior.woodbury_inv
        K_star = np.atleast_1d(self.kern.K(x, X_D))

        mean_hessian = np.einsum('ijkl,jm,m->ikl', kernel_hessian, K_D_inv, Y_D, optimize=True)

        diagonal_hessian = self._diagonal_hessian(x)
        data_dependent_hessian_half = np.einsum('iljk,lm,im->ijk', kernel_hessian, K_D_inv, K_star, optimize=True) \
                                + np.einsum('ilj,lm,imk->ijk', kernel_jacobian, K_D_inv, kernel_jacobian, optimize=True)
        data_dependent_hessian = data_dependent_hessian_half + np.swapaxes(data_dependent_hessian_half, -1, -2)

        variance_hessian = diagonal_hessian - data_dependent_hessian

        return mean_hessian, variance_hessian

    def update(self, x: ndarray, y: Union[ndarray, float]):
        """Add new data to the GP.

        Parameters
        ----------
        x
            A 2D array of shape (num_points, num_dimensions), or a 1D array of shape (num_dimensions).
        y
            A 1D array of shape (num_points). If X is 1D, this may also be a 0D array or float.

        Raises
        ------
        ValueError
            If the number of points in `x` does not equal the number of points in `y`.
        """
        x, y = _validate_and_transform_for_gpy_update(x, y)

        X = np.concatenate((self.X, x))
        Y = np.concatenate((self.Y, y))

        self.set_XY(X, Y)

    def _kernel_jacobian(self, x):
        return kernel_gradients.jacobian(self.kern, x, self.X)

    def _kernel_hessian(self, x):
        return kernel_gradients.hessian(self.kern, x, self.X)

    def _diagonal_hessian(self, x):
        return kernel_gradients.diagonal_hessian(self.kern, x)

    # noinspection PyUnusedLocal
    # This is called with a keyword argument "which" by GPy when the underlying GP is updated. We allow this to be
    # called with any set of arguments, but ignore them all.
    def _clear_cache(self, *args, **kwargs):
        clear_last_value_caches(self)

    @property
    def kernel(self) -> GPy.kern.Kern:
        return self._gpy_gp.kern


class WarpedGP(ABC):
    """Represents a Gaussian Process where the output space has been warped.

    Models of this type will make use of an underlying Gaussian Process model, and work with its outputs to produce a
    warped model. Instances of this class each have an instance of `GP` for this underlying model."""

    def __init__(self, gp: Union[GP, GPy.core.gp.GP]):
        """Create a Warped GP from a GP.

        Parameters
        ----------
        gp
            Either a `GPy.core.gp.GP`, which will be wrapped in a `GP`, or a `GP`.
        """
        if isinstance(gp, GP):
            self._gp = gp
        elif isinstance(gp, GPy.core.gp.GP):
            self._gp = GP(gp)
        else:
            raise ValueError("Argument to __init__ must be a GP.")

        self.dimensions = self._gp.dimensions

    @property
    def kernel(self) -> GPy.kern.Kern:
        return self._gp.kern

    @abstractmethod
    def posterior_mean_and_variance(self, x: ndarray) -> Tuple[ndarray, ndarray]:
        """Get the posterior mean and variance at a point, or a set of points.

        Parameters
        ----------
        x
            The point(s) at which to evaluate the posterior mean and variance. A 2D array of shape
            (num_points, num_dimensions), or a 1D array of shape (num_dimensions).

        Returns
        -------
        mean : ndarray
            A 1D array of shape (num_points) if the input was 2D, or a 0D array if the input was 1D. The :math:`i`-th
            element is the posterior mean at the :math:`i`-th point of `x`.
        variance : ndarray
            A 1D array of shape (num_points) if the input was 2D, or a 0D array if the input was 1D. The :math:`i`-th
            element is the posterior variance at the :math:`i`-th point of `x`.
        """

    @abstractmethod
    def posterior_variance_jacobian(self, x: ndarray) -> ndarray:
        """Get the jacobian of the posterior variance.

        Parameters
        ----------
        x
            The point(s) at which to evaluate the jacobian. A 2D array of shape (num_points, num_dimensions), or a 1D
            array of shape (num_dimensions).

        Returns
        -------
        jacobian : ndarray
            A 2D array of shape (num_points, num_dimensions) if the input was 2D, or a 1D array of shape
            (num_dimensions) if the input was 1D. The :math:`(i, j)`-th element is the :math:`j`-th component of the
            jacobian of the posterior variance at the :math:`i`-th point of `x`.
        """

    @abstractmethod
    def posterior_variance_hessian(self, x: ndarray) -> ndarray:
        """Get the hessian of the posterior variance.

        Parameters
        ----------
        x
            The point(s) at which to evaluate the hessian. A 2D array of shape (num_points, num_dimensions), or a 1D
            array of shape (num_dimensions).

        Returns
        -------
        hessian : ndarray
            A 3D array of shape (num_points, num_dimensions, num_dimensions) if the input was 2D, or a 2D array of shape
            (num_dimensions, num_dimensions) if the input was 1D. The :math:`(i, j, k)`-th element is the
            :math:`(j, k)`-th mixed partial derivative of the posterior variance at the :math:`i`-th point of `x`.
        """

    @abstractmethod
    def update(self, x: ndarray, y: ndarray):
        """Add new data to the GP.

        Parameters
        ----------
        x
            A 2D array of shape (num_points, num_dimensions), or a 1D array of shape (num_dimensions).
        y
            A 1D array of shape (num_points). If X is 1D, this may also be a 0D array or float.

        Raises
        ------
        ValueError
            If the number of points in `x` does not equal the number of points in `y`.
        """

    @abstractmethod
    def fantasise(self, x, y):
        """QQ"""

    @abstractmethod
    def remove_fantasies(self):
        """QQ"""


class WsabiLGP(WarpedGP):
    """An approximate model for a GP using a square-root warping of the output space, using a linearisation of the
    inverse warping.

    Notes
    -----
    This method, termed "WSABI-L", was introduced in [1]_ as one possible approximation to the square-root transform
    dubbed "WSABI".

    References
    ----------
    .. [1] Gunter, Tom, et al. "Sampling for inference in probabilistic models with fast Bayesian quadrature."
       Advances in neural information processing systems. 2014.
    """

    def __init__(self, gp: Union[GP, GPy.core.GP]):
        super().__init__(gp)

        self._alpha = 0.8 * min(*(gp.Y**2 / 2))
        self._true_alpha = self._alpha

        # We need to keep track of the original values of y, since whenever alpha changes, we'll need to apply the new
        # transform to the old data. We also keep track of the corresponding values of x separately here, since this
        # simplifies some operations.
        self._unwarped_Y = [gp.Y**2 / 2]
        self._all_X = [gp.X]

        self._fantasy_X = []
        self._fantasy_Y = []

    @flexible_array_dimensions
    def posterior_mean_and_variance(self, x: ndarray) -> Tuple[ndarray, ndarray]:
        """Get the posterior mean and variance at a point, or a set of points.

        Overrides :func:`~WarpedGP.posterior_mean_and_variance` - please see that method's documentation for further
        details on arguments and return values.
        """
        gp_mean, gp_variance = self._gp.posterior_mean_and_variance(x)

        mean = self._alpha + gp_mean ** 2 / 2
        variance = gp_variance * gp_mean ** 2

        return mean, variance

    @flexible_array_dimensions
    def posterior_variance_jacobian(self, x: ndarray) -> ndarray:
        """Get the jacobian of the posterior variance.

        Overrides :func:`~WarpedGP.posterior_variance_jacobian` - please see that method's documentation for further
        details on arguments and return values.

        Notes
        -----
        With the following notation:

            - :math:`X_i` for the :math:`i`-th point of the input array `x`
            - :math:`m_i` for the posterior mean of the underlying GP at :math:`X_i`
            - :math:`C_i` for the posterior variance of the underlying GP at :math:`X_i`
            - :math:`V_i` for the posterior variance of the WSABI-L model at :math:`X_i`

        we have :math:`V_i = m_i^2 C_i`.
        """
        gp_mean, gp_variance = self._gp.posterior_mean_and_variance(x)

        gp_mean_jacobian, gp_variance_jacobian = self._gp.posterior_jacobians(x)

        return jacobian_of_f_squared_times_g(
            f=gp_mean, f_jacobian=gp_mean_jacobian,
            g=gp_variance, g_jacobian=gp_variance_jacobian)

    @flexible_array_dimensions
    def posterior_variance_hessian(self, x: ndarray) -> ndarray:
        """Get the hessian of the posterior variance.

        Overrides :func:`~WarpedGP.posterior_variance_hessian` - please see that method's documentation for further
        details on arguments and return values.

        Notes
        -----
        With the following notation:

            - :math:`X_i` for the :math:`i`-th point of the input array `x`
            - :math:`m_i` for the posterior mean of the underlying GP at :math:`X_i`
            - :math:`C_i` for the posterior variance of the underlying GP at :math:`X_i`
            - :math:`V_i` for the posterior variance of the WSABI-L model at :math:`X_i`

        we have :math:`V_i = m_i^2 C_i`.
        """
        gp_mean, gp_variance = self._gp.posterior_mean_and_variance(x)

        gp_mean_jacobian, gp_variance_jacobian = self._gp.posterior_jacobians(x)
        gp_mean_hessian, gp_variance_hessian = self._gp.posterior_hessians(x)

        return hessian_of_f_squared_times_g(
            f=gp_mean, f_jacobian=gp_mean_jacobian, f_hessian=gp_mean_hessian,
            g=gp_variance, g_jacobian=gp_variance_jacobian, g_hessian=gp_variance_hessian)

    def update(self, x: ndarray, y: ndarray):
        """Add new data to the GP. If necessary, this will also update the parameter alpha to a value consistent with
        the new data.

        Overrides :func:`~WarpedGP.update` - please see that method's documentation for further details on arguments and
        return values.
        """
        # Since we may need to directly modify the existing X and Y data on the underlying GP, we can't rely on the
        # update method of `GP` to deal with all updates here, so we need to apply the same validation and
        # transformation for dealing with the GPy data directly.
        x, y = _validate_and_transform_for_gpy_update(x, y)

        self._all_X.append(x)
        self._unwarped_Y.append(y)

        new_min = min(self._alpha, *(0.8 * y))

        if new_min is not self._alpha:
            self._update_alpha_and_reprocess_data(new_min)
        else:
            warped_y = self._warp(y)
            self._gp.update(x, warped_y)

    def fantasise(self, x, y):
        x, y = _validate_and_transform_for_gpy_update(x, y)

        self._fantasy_X.append(x)
        self._fantasy_Y.append(y)

        new_min = min(self._alpha, *(0.8 * y))

        if new_min is not self._alpha:
            self._fantasise_alpha_and_reprocess_data(new_min)
        else:
            warped_y = self._warp(y)
            self._gp.update(x, warped_y)

    def _fantasise_alpha_and_reprocess_data(self, alpha: float):
        self._alpha = alpha

        warped_Y = [self._warp(y) for y in self._unwarped_Y] + [self._warp(y) for y in self._fantasy_Y]
        all_warped_Y = np.concatenate(warped_Y)

        all_X = np.concatenate(self._all_X + self._fantasy_X)

        self._gp.set_XY(all_X, all_warped_Y)

    def remove_fantasies(self):
        self._alpha = self._true_alpha

        warped_Y = [self._warp(y) for y in self._unwarped_Y]
        all_warped_Y = np.concatenate(warped_Y)

        all_X = np.concatenate(self._all_X)

        self._gp.set_XY(all_X, all_warped_Y)

        self._fantasy_X = []
        self._fantasy_Y = []

    def _warp(self, y: ndarray) -> ndarray:
        return np.sqrt(2 * (y - self._alpha))

    def _update_alpha_and_reprocess_data(self, alpha: float):
        self._alpha = alpha
        self._true_alpha = alpha

        warped_Y = [self._warp(y) for y in self._unwarped_Y]
        all_warped_Y = np.concatenate(warped_Y)

        all_X = np.concatenate(self._all_X)

        self._gp.set_XY(all_X, all_warped_Y)


def _validate_and_transform_for_gpy_update(x: ndarray, y: ndarray) -> Tuple[ndarray, ndarray]:
    """Ensure that x and y have the right dimensionality and size to be passed to `GPy.core.gp.GP.set_XY`."""
    x = np.atleast_2d(x)

    if not isinstance(y, ndarray):
        y = np.array(y)

    # GPy expects y to have shape (num_points, 1)
    y = y.reshape(-1, 1)

    x_points, y_points = np.size(x, axis=0), np.size(y, axis=0)

    if x_points != y_points:
        raise ValueError("The number of points in x (i.e. the size of the first dimension) must equal the number "
                         "of points in y. x contained {} points, y contained {} points.".format(x_points, y_points))

    return x, y
