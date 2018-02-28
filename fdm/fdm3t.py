
import numpy as np
import matplotlib.pyplot as plt
import scipy.sparse as sp
from scipy.interpolate import interp1d
from scipy.sparse.linalg import spsolve # to use its short name
from colors import colors


class InputError(Exception):
    pass

NOT = np.logical_not

def fdm3t(gr=None, t=None, kxyz=None, Ss=None,
          FQ=None, HI=None, IBOUND=None, epsilon=0.67):
    '''Transient 3D Finite Difference Model returning computed heads and flows.

    Heads and flows are returned as 3D arrays as specified under output parmeters.

    Parameters
    ----------
    'gr' : `grid_object`, generated by gr = Grid(x, y, z, ..)
        if `gr.axial`==True, then the model is run in axially symmetric model
    t : ndarray, shape: [Nt+1]
        times at which the heads and flows are desired including the start time,
        which is usually zero, but can have any value.
    `kx`, `ky`, `kz` : ndarray, shape: (Ny, Nx, Nz), [L/T]
        hydraulic conductivities along the three axes, 3D arrays.
    `Ss` : ndarray, shape: (Ny, Nx, Nz), [L-1]
        specific elastic storage
    `FQ` : ndarray, shape: (Ny, Nx, Nz), [L3/T]
        prescrived cell flows (injection positive, zero of no inflow/outflow)
    `IH` : ndarray, shape: (Ny, Nx, Nz), [L]
        initial heads. `IH` has the prescribed heads for the cells with prescribed head.
    `IBOUND` : ndarray, shape: (Ny, Nx, Nz) of int
        boundary array like in MODFLOW with values denoting
        * IBOUND>0  the head in the corresponding cells will be computed
        * IBOUND=0  cells are inactive, will be given value NaN
        * IBOUND<0  coresponding cells have prescribed head
    `epsilon` : float, dimension [-]
        degree of implicitness, choose value between 0.5 and 1.0

    outputs
    -------
    `out` : namedtuple containing heads and flows:
        `out.Phi` : ndarray, shape: (Nt+1, Ny, Nx, Nz), [L3/T]
            computed heads. Inactive cells will have NaNs
            To get heads at time t[i], use Out.Phi[i]
            Out.Phi[0] = initial heads
        `out.Q`   : ndarray, shape: (Nt, Ny, Nx, Nz), [L3/T]
            net inflow in all cells during time step, inactive cells have 0
            Q during time step i, use Out.Q[i]
        `out.Qs`  : ndarray, shape: (Nt, Ny, Nx, Nz), [L3/T]
            release from storage during time step.
        `out.Qx   : ndarray, shape: (Nt, Ny, Nx-1, Nz), [L3/T]
            intercell flows in x-direction (parallel to the rows)
        `out.Qy`  : ndarray, shape: (Nt, Ny-1, Nx, Nz), [L3/T]
            intercell flows in y-direction (parallel to the columns)
        `out.Qz`  : ndarray, shape: (Nt, Ny, Nx, Nz-1), [L3/T]
            intercell flows in z-direction (vertially upward postitive)

    TO 161024
    '''

    if gr.axial:
        print('Running in axial mode, y-values are ignored.')

    if isinstance(kxyz, tuple):
        kx, ky, kz = kxyz
    else:
        kx = ky = kz = kxyz

    if kx.shape != gr.shape:
        raise AssertionError("shape of kx {0} differs from that of model {1}".format(kx.shape,gr.shape))
    if ky.shape != gr.shape:
        raise AssertionError("shape of ky {0} differs from that of model {1}".format(ky.shape,gr.shape))
    if kz.shape != gr.shape:
        raise AssertionError("shape of kz {0} differs from that of model {1}".format(kz.shape,gr.shape))
    if Ss.shape != gr.shape:
        raise AssertionError("shape of Ss {0} differs from that of model {1}".format(Ss.shape,gr.shape))

    kx[kx<1e-20] = 1e-20
    ky[ky<1e-20] = 1e-20
    kz[kz<1e-20] = 1e-20

    active = (IBOUND >0).reshape(gr.nod,)  # boolean vector denoting the active cells
    inact  = (IBOUND==0).reshape(gr.nod,)  # boolean vector denoting inacive cells
    fxhd   = (IBOUND <0).reshape(gr.nod,)  # boolean vector denoting fixed-head cells

    # reshaping shorthands
    dx = np.reshape(gr.dx, (1, 1, gr.nx))
    dy = np.reshape(gr.dy, (1, gr.ny, 1))

    # half cell flow resistances
    if not gr.axial:
        Rx1 = 0.5 *    dx / (   dy * gr.DZ) / kx
        Rx2 = Rx1
        Ry1 = 0.5 *    dy / (gr.DZ *    dx) / ky
        Rz1 = 0.5 * gr.DZ / (   dx *    dy) / kz
    else:
        # prevent div by zero warning in next line; has not effect because x[0] is not used
        x = gr.x.copy();  x[0] = x[0] if x[0]>0 else 0.1* x[1]

        Rx1 = 1 / (2 * np.pi * kx * gr.DZ) * np.log(x[1:] /  gr.xm).reshape((1, 1, gr.nx))
        Rx2 = 1 / (2 * np.pi * kx * gr.DZ) * np.log(gr.xm / x[:-1]).reshape((1, 1, gr.nx))
        Ry1 = np.inf * np.ones(gr.shape)
        Rz1 = 0.5 * gr.DZ / (np.pi * (gr.x[1:]**2 - gr.x[:-1]**2).reshape((1, 1, gr.nx)) * kz)

    # set flow resistance in inactive cells to infinite
    Rx1[inact.reshape(gr.shape)] = np.inf
    Rx2[inact.reshape(gr.shape)] = np.inf
    Ry1[inact.reshape(gr.shape)] = np.inf
    Ry2 = Ry1
    Rz1[inact.reshape(gr.shape)] = np.inf
    Rz2 = Rz1

    # conductances between adjacent cells
    Cx = 1 / (Rx1[: , :,1:] + Rx2[:  ,:  ,:-1])
    Cy = 1 / (Ry1[: ,1:, :] + Ry2[:  ,:-1,:  ])
    Cz = 1 / (Rz1[1:, :, :] + Rz2[:-1,:  ,:  ])

    # storage term, variable dt not included
    Cs = (Ss * gr.Volume / epsilon).ravel()

    # cell number of neighboring cells
    IW = gr.NOD[:,:,:-1]  # east neighbor cell numbers
    IE = gr.NOD[:,:, 1:] # west neighbor cell numbers
    IN = gr.NOD[:,:-1,:] # north neighbor cell numbers
    IS = gr.NOD[:, 1:,:]  # south neighbor cell numbers
    IT = gr.NOD[:-1,:,:] # top neighbor cell numbers
    IB = gr.NOD[ 1:,:,:]  # bottom neighbor cell numbers

    R = lambda x : x.ravel()  # generate anonymous function R(x) as shorthand for x.ravel()

    # notice the call  csc_matrix( (data, (rowind, coind) ), (M,N))  tuple within tupple
    # also notice that Cij = negative but that Cii will be postive, namely -sum(Cij)
    A = sp.csc_matrix(( np.concatenate(( R(Cx), R(Cx), R(Cy), R(Cy), R(Cz), R(Cz)) ),\
                        (np.concatenate(( R(IE), R(IW), R(IN), R(IS), R(IB), R(IT)) ),\
                         np.concatenate(( R(IW), R(IE), R(IS), R(IN), R(IT), R(IB)) ),\
                      )),(gr.nod,gr.nod))

    A = -A + sp.diags( np.array(A.sum(axis=1)).ravel() ) # Change sign and add diagonal

    #Initialize output arrays (= memory allocation)
    Nt = len(t)-1
    Phi = np.zeros((Nt+1, gr.nod)) # Nt+1 times
    Q   = np.zeros((Nt  , gr.nod)) # Nt time steps
    Qs  = np.zeros((Nt  , gr.nod))
    Qx  = np.zeros((Nt, gr.nz, gr.ny, gr.nx-1))
    Qy  = np.zeros((Nt, gr.nz, gr.ny-1, gr.nx))
    Qz  = np.zeros((Nt, gr.nz-1, gr.ny, gr.nx))

    # reshape input arrays to vectors for use in system equation
    FQ = R(FQ);  HI = R(HI);  Cs = R(Cs)

    # initialize heads
    Phi[0] = HI

    # solve heads at active locations at t_i+eps*dt_i

    Nt=len(t)  # for heads, at all times Phi at t[0] = initial head
    Ndt=len(np.diff(t)) # for flows, average within time step

    for idt, dt in enumerate(np.diff(t)):

        it = idt+1

        # this A is not complete !!
        RHS = FQ - (A + sp.diags(Cs / dt))[:,fxhd].dot(Phi[it-1][fxhd]) # Right-hand side vector

        Phi[it][active] = spsolve( (A + sp.diags(Cs / dt))[active][:,active],
                                  RHS[active] + Cs[active] / dt * Phi[it-1][active])

        # net cell inflow
        Q[idt]  = A.dot(Phi[it])

        Qs[idt] = -Cs/dt * (Phi[it]-Phi[it-1])


        #Flows across cell faces
        Qx[idt] =  -np.diff( Phi[it].reshape(gr.shape), axis=2) * Cx
        Qy[idt] =  +np.diff( Phi[it].reshape(gr.shape), axis=1) * Cy
        Qz[idt] =  +np.diff( Phi[it].reshape(gr.shape), axis=0) * Cz

        # update head to end of time step
        Phi[it][active] = Phi[it-1][active] + (Phi[it]-Phi[it-1])[active]/epsilon
        Phi[it][fxhd]   = Phi[it-1][fxhd]
        Phi[it][inact] = np.nan

    # reshape Phi to shape of grid
    Phi = Phi.reshape((Nt,) + gr.shape)
    Q   = Q.reshape( (Ndt,) + gr.shape)
    Qs  = Qs.reshape((Ndt,) + gr.shape)

    return {'t': t, 'Phi': Phi, 'Q': Q, 'Qs': Qs, 'Qx': Qx, 'Qy': Qy, 'Qz': Qz}




class Fdm3t:

    def __init__(self, gr=None, t=None, kxyz=None, Ss=None, FQ=None, HI=None,
                 IBOUND=None, epsilon=1.0):
        '''Set-up and run a 3D transient axially symmetric groundwater
        finite difference model.

        All model arrays must have shape [nlay, nrow, ncol]. This is so
        even if an axially symmetric model has only one row.

        parameters
        ----------
            gr : mfgrid.Grid object
                contains the computation finite difference network.
            t : time, numpy.ndarray
                if 0 is not included, it will be added to store
                the initial heads for t=0.
            kxyz : tuple of (Kr, Kz) or (Kr, Ky, Kz)
                Ky is ignored.
            Ss : ndarray of gr.shape
                Specifici storage coefficients.
            FQ : ndarray of gr.shape
                fixed infiltration per modflow cell. Extractoins are
                negative.
                This could be specified as a dict[isp] where isp is the
                stress-period number.
            HI : ndarray of gr.shape.
                initial heads and fixed heads.
                The heads are fixed where IBOUND<0.
                This could be specified as a dict[isp] where isp is the
                stress-period number. In that case, the heads at the
                nodes with IBOUND<0 will be replaced by the fixed heads
                speciied for the stress period.
            IBOUND : ndarray of dtype int of size gr.shape
                Boundary array like in MODFLOW. <0 means head is
                prescribed for the cell; 0 means cell is inactive, >0 means,
                head will be compute for the cell.
            epsilon : float between 0.5 and 1.
                Implicitness. 0.5 is indifferently stable (Crank Nicholson
                scheme of updating future head, most accurate), 1 is completely
                implicit, most stable, but sometimes a bit less accurate.e
                Modflow uses epsilon=1 implicitly.
        returns
        -------
            self.out : dictionary
                the simulation results: Phi, Q, Qx, Qy, Qz, Qs
                ndarrays with time as first dimension.
        '''

        if isinstance(kxyz, (tuple, list)):
            if len(kxyz)==3:
                Kh, _, Kv = kxyz
            elif len(kxyz) == 2:
                Kh, Kv = kxyz
            elif len(kxyz) == 1:
                Kh = kxyz[0]
                Kv = Kh
            else:
                raise ValueError("Can't understand input kxyz, use (Kx, Kz) tuple")
        else:
            Kh = kxyz
            Kv = kxyz

        assert np.all(gr.shape == Kh.shape), 'gr.shape != Kh.shape'
        assert np.all(gr.shape == Kv.shape), 'gr.shape != Kv.shape'
        assert np.all(gr.shape == FQ.shape), 'gr.shape != FQ.shape'
        assert np.all(gr.shape == Ss.shape), 'gr.shape != HI.shape'
        assert np.all(gr.shape == IBOUND.shape), 'gr.shape != IBOUND.shape'

        self.gr = gr
        self.t  = t
        self.Kh = Kh
        self.Kv = Kv
        self.Ss = Ss
        self.FQ = FQ
        self.HI = HI
        self.IBOUND = IBOUND

        # Immediately runs the model and stores its output
        self.out = fdm3t(gr=self.gr, t=self.t,
                         kxyz=(self.Kh, self.Kh, self.Kv),
                         Ss=self.Ss, FQ=self.FQ, HI=self.HI,
                         IBOUND=IBOUND, epsilon=1.0)

        print('Model was run see model.out, where model is your model name.')

    @property
    def obsNames(self):
        return [p[0] for p in self.points]
    @property
    def r_ow(self):
        return [p[1] for p in self.points]
    @property
    def z_ow(self):
        return [p[2] for p in self.points]


    def show(self, points, **kwargs):
        '''Plot the time-drawdown curves for the observation points.

        parameters
        ----------
            points: list of tuples of (name, r, z)
                names and position of each observation point.

        returns
        -------
            ax : Axis

        '''

        self.points = points


        layNr = self.gr.lrc(self.r_ow, np.zeros_like(self.r_ow), self.z_ow)[:, 0]

        # Numerical solutions interpolated at observation points

        # Get itnepolator but also squeeze out axis 2 (y)
        interpolator = interp1d(self.gr.xm, self.out['Phi'][:, :, 0, :], axis=2)

        # interpolate at radius of observation points
        phi_t = interpolator(self.r_ow)

        # prepare fance selection of iz, ix combinations of obs points
        Ipnt = np.arange(phi_t.shape[-1], dtype=int) # nr of obs points

        phi_t = phi_t[:, layNr, Ipnt] # fancy selection, all times

        ax = kwargs.pop('ax', None)
        if ax is None:
            fig, ax = plt.subplots()
            ax.set_title('Berekend stijghoogteverloop (axiaal model)')
            ax.set_xlabel('t [d]')
            ax.set_ylabel('drawdown [m]')
            ax.grid(True)

        size_inches = kwargs.pop('size_inches', None)
        title  = kwargs.pop('title', None)
        xscale = kwargs.pop('xscale', None)
        yscale = kwargs.pop('yscale', None)
        grid   = kwargs.pop('grid'  , None)

        if size_inches: fig.set_size_inches(size_inches)
        if title: ax.set_title(title)
        if xscale: ax.set_xscale(xscale)
        if yscale: ax.set_yscale(yscale)
        if grid:   ax.grid(grid)

        # Numeric, fdm
        for fi, label, r, z, color in zip(
                phi_t.T, self.obsNames, self.r_ow, self.z_ow, colors):
            ax.plot(self.t[1:], fi[1:], color=color,
                    label='{:6}, r={:>5.0f} m, z={:>3.0f} m'.format(label,r,z),
                     **kwargs)

        ax.legend(loc='best')

        return ax


    def get_psi(self):
        '''Comopute stream function and store in self.psi.

        stores psi in self.psi

        returns
        -------
            psi (values are in m2/d)

        '''


        Qx = self.out['Qx'][-1][:, 0, :]

        psi = np.cumsum(np.vstack((Qx,
                                   np.zeros_like(Qx[-1:])))[::-1], axis=0)[::-1]
        self.psi = psi

        return psi


    def contour(self, dphi=None, dpsi=None, **kwargs):
        ''''Plot head and contours with streamlines.

        parameters
        ----------
            dphi : float [m]
                head difference between successive contour lines.
            dpsi : float [m2/d]
                amount of water flowing between adjacent stream lines
            additional kwargs:
                passed on to contour fuctions
        '''

        patches = kwargs.pop('patches', None)


        title = kwargs.pop('title',
                   'Stijghoogten en stroomfunctie.')

        if dphi is not None:
            phi = self.out['Phi'][-1][:, 0, :]
            fmin = np.min(phi[np.logical_not(np.isnan(phi))])
            fmax = np.max(phi[np.logical_not(np.isnan(phi))])
            philevels = np.arange(np.floor(fmin), np.ceil(fmax), dphi)

            title + ' dphi={:.3g}m.'.format(dphi)

        if dpsi is not None:
            self.get_psi()
            pmin = np.min(self.psi)
            pmax = np.max(self.psi)
            psilevels = np.arange(np.floor(pmin), np.ceil(pmax), dpsi)

            title + ' dpsi={:.3g}m2/d'.format(dpsi)


        fig, ax = plt.subplots()
        size_inches = kwargs.pop('size_inches', None)
        if not size_inches is None:
            fig.set_size_inches(size_inches)

        ax.set_title(title)
        ax.set_xlabel('r [m]')
        ax.set_ylabel('z [m]')
        ax.grid()

        xlim   = kwargs.pop('xlim',   None)
        ylim   = kwargs.pop('ylim',   None)
        xscale = kwargs.pop('xscale', None)
        yscale = kwargs.pop('yscale', None)

        if not xlim is None: ax.set_xlim(xlim)
        if not ylim is None: ax.set_ylim(ylim)
        if not xscale is None: ax.set_xscale(xscale)
        if not yscale is None: ax.set_yscale(yscale)

        if dphi is not None:
            ax.contour(self.gr.xm,     self.gr.zc, phi, philevels, **kwargs)
        if dpsi is not None:
            ax.contour(self.gr.x[1:-1], self.gr.z, self.psi, psilevels,
                       linestyles='-', colors='b')

        if patches is not None:
            for p in patches:
                ax.add_patch(p)

        return ax


