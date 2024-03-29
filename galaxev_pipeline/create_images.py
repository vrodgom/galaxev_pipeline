"""
Create synthetic images of Illustris/TNG galaxies using precalculated
magnitudes from GALAXEV models (see stellar_photometrics.py)
following the methodology of Rodriguez-Gomez et al. (2019).
"""
# Author: Vicente Rodriguez-Gomez <vrodgom.astro@gmail.com>
# Licensed under a 3-Clause BSD License.
import numpy as np
import h5py
import os
import sys
import scipy.interpolate as ip
from scipy.spatial import cKDTree
from astropy.io import fits
from astropy.cosmology import FlatLambdaCDM
import time
import ctypes

import illustris_python as il

KILL_TAG = 1
WORK_TAG = 0
parttype_stars = 4


def get_fluxes(initial_masses_Msun, metallicities, stellar_ages_yr, filter_name):
    """
    Return "fluxes" in units of maggies (following SDSS nomenclature) in the
    AB system, using the BC03 tables generated by stellar_photometrics.py.
    In other words, these fluxes are normalized with respect to the reference
    flux that would have a magnitude of zero in the AB system (zero-point = 0)
    and can be converted to magnitudes as MAG = -2.5 * log10(DATA).
    """
    if use_cf00:
        filename_bc03 = '%s/stellar_photometrics_cf00_%03d.hdf5' % (
            suitedir, snapnum)
    else:
        filename_bc03 = '%s/stellar_photometrics_%03d.hdf5' % (
            suitedir, snapnum)

    with h5py.File(filename_bc03, 'r') as f_bc03:
        bc03_metallicities = f_bc03['metallicities'][:]
        bc03_stellar_ages = f_bc03['stellar_ages'][:]
        bc03_magnitudes = f_bc03[filter_name][:]

    spline = ip.RectBivariateSpline(
        bc03_metallicities, bc03_stellar_ages, bc03_magnitudes, kx=1, ky=1, s=0)

    # BC03 magnitudes are normalized to a mass of 1 Msun:
    magnitudes = spline.ev(metallicities, stellar_ages_yr)  # for 1 Msun

    # Take into account the initial masses of the stellar particles:
    magnitudes -= 2.5 * np.log10(initial_masses_Msun)

    # Convert apparent magnitudes to fluxes in "maggies" (see docstring):
    fluxes = 10.0**(-2.0/5.0*magnitudes)

    return fluxes


def transform(x, proj_kind='xy', jvec=None):
    """
    Return a projection of the particle positions. In all cases we only
    return the first two coordinates (x, y).

    Parameters
    ----------
    x : array-like
        2-dimensional array (Nx3) with the particle positions.
    proj_kind : str, optional
        Specify which kind of projection. The possible values are:

        'yz' : Project onto the yz plane.
        'zx' : Project onto the zx plane.
        'xy' : Project onto the xy plane.
        'planar' : Rotate around z axis until jvec is in the yz plane. Return xy.
        'edgeon' : jvec perpendicular to z-axis.
        'faceon' : jvec parallel to z-axis.

    jvec : array-like, optional
        Direction of the galaxy's stellar angular momentum.
        Does not need to be normalized to 1 (this is done anyway).

    Returns
    -------
    x_new : array-like
        2-dimensional array (Nx2) with the projected particle positions.

    """
    if jvec is not None:
        assert len(jvec) == x.shape[1] == 3

    # Define unit basis vectors of new reference frame
    if proj_kind == 'xy':
        e1 = np.array([1.0, 0.0, 0.0])
        e2 = np.array([0.0, 1.0, 0.0])
    elif proj_kind == 'yz':
        e1 = np.array([0.0, 1.0, 0.0])
        e2 = np.array([0.0, 0.0, 1.0])
    elif proj_kind == 'zx':
        e1 = np.array([0.0, 0.0, 1.0])
        e2 = np.array([1.0, 0.0, 0.0])
    elif proj_kind == 'planar':
        if jvec is None:
            raise Exception('jvec must be specified for this projection.')
        # New y-axis is the projection of jvec onto the xy plane
        e2 = np.array([jvec[0], jvec[1], 0.0])
        e2 = e2 / np.linalg.norm(e2)  # normalize
        e1 = np.cross(e2, np.array([0.0, 0.0, 1.0]))
    elif proj_kind == 'edgeon':
        if jvec is None:
            raise Exception('jvec must be specified for this projection.')
        # New y-axis is aligned with jvec
        e2 = jvec[:] / np.linalg.norm(jvec)  # normalize
        e1 = np.cross(e2, np.array([0.0, 0.0, 1.0]))
    elif proj_kind == 'faceon':
        if jvec is None:
            raise Exception('jvec must be specified for this projection.')
        # New z-axis is aligned with jvec
        e3 = jvec[:] / np.linalg.norm(jvec)  # normalize
        # New x-axis is chosen to coincide with edge-on projection
        e1 = np.cross(e3, np.array([0.0, 0.0, 1.0]))
        e2 = np.cross(e3, e1)
    else:
        raise Exception('Projection kind not understood.')

    # Project onto new axes
    x_new = np.zeros((x.shape[0], 2), dtype=np.float64)
    x_new[:, 0] = np.dot(x, e1)
    x_new[:, 1] = np.dot(x, e2)

    return x_new


def get_hsml(pos, num_neighbors):
    """
    Get distance to the Nth (usually 32nd) nearest neighbor in 3D.

    Parameters
    ----------
    pos : array-like
        2-dimensional array (Nx3) with the coordinates of the particles.
    num_neighbors : int
        Specifies how many neighbors to search for.

    Returns
    -------
    hsml : array-like
        Distances to the Nth nearest neighbors.

    """
    tree = cKDTree(pos)
    res = tree.query(pos, k=num_neighbors + 1)
    hsml = res[0][:, -1]

    return hsml


def adaptive_smoothing(x, y, hsml, xcenters, ycenters, num_rhalfs, codedir,
                       weights=None):
    """
    Do adaptive smoothing similar to Torrey et al. (2015).

    Parameters
    ----------
    x : array-like
        x-coordinates of the particles in units of rhalf.
    y : array-like
        y-coordinates of the particles in units of rhalf.
    hsml : array-like
        Smoothing lengths (same units as x and y).
    xcenters : array-like
        1-d array with the pixel centers along the x-axis.
    ycenters : array-like
        1-d array with the pixel centers along the y-axis.
    num_rhalfs : scalar
        Number of stellar half-mass radii on each side from the center.
        The coordinates are restricted to (-num_rhalfs, num_rhalfs).
    codedir : str
        Directory with the galaxev_pipeline code, where the compiled
        adaptive smoothing module should be found.
    weights : array-like, optional
        Array of the same size as ``x`` and ``y`` with the particle
        weights, e.g., particle masses or fluxes. If ``None``, the
        particle number density is calculated.

    Returns
    -------
    H : array-like
        A 2D array with the density at each pixel center.

    """
    assert x.shape == y.shape
    if weights is None:
        weights = np.ones_like(x)

    # Make everything double
    x = np.float64(x)
    y = np.float64(y)
    hsml = np.float64(hsml)
    weights = np.float64(weights)

    # Ignore out-of-range particles
    locs_withinrange = (np.abs(x) < num_rhalfs) | (np.abs(y) < num_rhalfs)
    x = x[locs_withinrange]
    y = y[locs_withinrange]
    hsml = hsml[locs_withinrange]
    weights = weights[locs_withinrange]

    # Compile as:
    # gcc -o adaptive_smoothing.so -shared -fPIC adaptive_smoothing.c
    sphlib = np.ctypeslib.load_library('adaptive_smoothing', codedir)
    sphlib.add.restype = None
    sphlib.add.argtypes = [
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
        ctypes.c_double,
    ]

    if verbose:
        print('Doing adaptive smoothing...')

    X, Y = np.meshgrid(xcenters, ycenters)
    ny, nx = X.shape
    Y_flat, X_flat = Y.ravel(), X.ravel()
    Z_flat = np.zeros_like(X_flat)
    sphlib.add(
        X_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        Y_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        Z_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.c_int(nx),
        ctypes.c_int(ny),
        x.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        y.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        weights.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        hsml.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.c_int(x.size),
        ctypes.c_double(num_rhalfs))

    H = Z_flat.reshape(X.shape)

    return H


def get_subfind_ids(snapnum):
    """
    Return the Subfind IDs of galaxies with stellar and/or halo masses
    above some specified minimum value(s).
    """
    nsubs = len(sub_gr_nr)
    locs_valid = np.ones(nsubs, dtype=np.bool8)

    if log_mstar_min > 0:
        print('Loading galaxy stellar masses...')
        start = time.time()
        mstar = il.groupcat.loadSubhalos(
            basedir, snapnum, fields=['SubhaloMassType'])[:, parttype_stars]
        print('Time: %g s.' % (time.time() - start))
        mstar_min = 10.0**log_mstar_min / 1e10 * h  # 10^10 Msun/h
        locs_valid &= mstar >= mstar_min

    if log_m200_min > 0:
        print('Loading halo masses...')
        start = time.time()
        group_m200 = il.groupcat.loadHalos(
            basedir, snapnum, fields=['Group_M_Crit200'])
        print('Time: %g s.' % (time.time() - start))
        m200_min = 10.0**log_m200_min / 1e10 * h  # 10^10 Msun/h
        locs_valid &= group_m200[sub_gr_nr] >= m200_min

    if centrals_only:
        print('Selecting centrals only...')
        start = time.time()
        group_first_sub = il.groupcat.loadHalos(
            basedir, snapnum, fields=['GroupFirstSub'])
        print('Time: %g s.' % (time.time() - start))
        is_central = group_first_sub[sub_gr_nr] == np.arange(nsubs, dtype=np.uint32)
        locs_valid &= is_central

    subfind_ids = np.flatnonzero(locs_valid)
    return np.array(subfind_ids, dtype=np.int32)


def get_num_rhalfs_npixels(subfind_id):
    """
    Helper function to get the current values of num_rhalfs and npixels,
    considering that one and only one of num_rhalfs, num_r200, and npixels
    is defined.

    Note that npixels refers to the total number of pixels per side, while
    num_rhalfs and num_r200 are measured from the center of the image (i.e.,
    an image with num_rhalfs = 7.5 would roughly measure 15.0 * rhalf on
    each side).
    """
    if npixels > 0:
        cur_npixels = npixels
    elif num_rhalfs > 0:
        cur_npixels = int(np.ceil(
            2.0 * num_rhalfs * sub_rhalf[subfind_id] / ckpc_h_per_pixel))
    elif num_r200 > 0:
        cur_npixels = int(np.ceil(
            2.0 * num_r200 * group_r200[sub_gr_nr[subfind_id]] / ckpc_h_per_pixel))
    else:
        raise Exception(
            "One (and only one) of num_rhalfs, num_r200, and npixels " +
            "should be specified (the others should be -1).")

    # In any case, we "update" num_rhalfs so that 2.0 * num_rhalfs corresponds
    # exactly to an integer number of pixels:
    assert sub_rhalf[subfind_id] > 0
    cur_num_rhalfs = cur_npixels * ckpc_h_per_pixel / (2.0 * sub_rhalf[subfind_id])

    return cur_num_rhalfs, cur_npixels


def create_image_single_sub(subfind_id, pos, hsml_ckpc_h, fluxes):
    """
    Create synthetic image for a single subhalo.

    Note that (for historical reasons) we work in units of rhalf.
    """
    cur_num_rhalfs, cur_npixels = get_num_rhalfs_npixels(subfind_id)
    if verbose:
        print('cur_num_rhalfs = %.1f' % (cur_num_rhalfs,))
        print('cur_npixels = %d' % (cur_npixels,))

    # Periodic boundary conditions (center at most bound stellar particle)
    dx = pos[:] - sub_pos[subfind_id]
    dx = dx - (np.abs(dx) > 0.5 * box_size) * np.copysign(box_size, dx)

    # Normalize by rhalf
    dx = dx / sub_rhalf[subfind_id]
    hsml = hsml_ckpc_h / sub_rhalf[subfind_id]

    # Transform particle positions according to 'proj_kind' (2D projection)
    if require_jstar:
        dx_new = transform(dx, proj_kind=proj_kind, jvec=jstar_direction[subfind_id])
    else:
        dx_new = transform(dx, proj_kind=proj_kind)

    # Define 2D bins (in units of rhalf)
    xedges = np.linspace(-cur_num_rhalfs, cur_num_rhalfs, num=cur_npixels+1)
    yedges = np.linspace(-cur_num_rhalfs, cur_num_rhalfs, num=cur_npixels+1)
    xcenters = 0.5 * (xedges[:-1] + xedges[1:])
    ycenters = 0.5 * (yedges[:-1] + yedges[1:])

    # Store images here
    image = np.zeros((num_filters, cur_npixels, cur_npixels), dtype=np.float32)

    # Iterate over broadband filters
    for i, filter_name in enumerate(filter_names):
        image[i, :, :] = adaptive_smoothing(
            dx_new[:, 0], dx_new[:, 1], hsml, xcenters, ycenters,
            cur_num_rhalfs, codedir, weights=fluxes[i, :])

    # Convert image units from maggies/rhalf^2 to maggies/pixel_size^2
    pixel_size_rhalfs = 2.0 * cur_num_rhalfs / float(cur_npixels)  # rhalfs
    image *= pixel_size_rhalfs**2

    # Create some header attributes
    header = fits.Header()
    pc_per_pixel = ckpc_h_per_pixel * 1000.0 / h / (1.0 + z)  # physical pc
    header["BUNIT"] = ("maggies/pixel", "Unit of the array values")
    header["CDELT1"] = (pc_per_pixel, "Coordinate increment along X-axis")
    header["CTYPE1"] = ("pc", "Physical units of the X-axis increment")
    header["CDELT2"] = (pc_per_pixel, "Coordinate increment along Y-axis")
    header["CTYPE2"] = ("pc", "Physical units of the Y-axis increment")
    header["PIXSCALE"] = (arcsec_per_pixel, "Pixel size in arcsec")
    header["USE_Z"] = (use_z, "Observed redshift of the source")
    for k in range(num_filters):
        header["FILTER%d" % (k,)] = (
            filter_names[k], "Broadband filter index = %d" % (k,))

    # Write to FITS file
    hdu = fits.PrimaryHDU(data=image, header=header)
    hdulist = fits.HDUList([hdu])
    hdulist.writeto('%s/broadband_%d.fits' % (datadir, subfind_id))
    hdulist.close()

    if verbose:
        print('Finished for subhalo %d.\n' % (subfind_id,))


def create_images(object_id):
    """
    Create (adaptively smoothed) images for the chosen filters.
    Consider all relevant subhalos of a given FoF group.

    Parameters
    ----------
    object_id : int
        The ID of the object of interest. If ``use_fof`` is True,
        this corresponds to the FoF group ID. Otherwise, this is
        the subhalo ID.
    """
    if use_fof:
        # Get Subfind IDs that belong to the current FoF group.
        fof_subfind_ids = subfind_ids[fof_ids == object_id]
    else:
        # A bit of a hack. This way we only process the subhalo of
        # interest without rewriting too much code.
        fof_subfind_ids = np.array([object_id], dtype=np.int32)

    # Load stellar particle info
    fields = ['Coordinates', 'GFM_InitialMass', 'GFM_Metallicity',
              'GFM_StellarFormationTime']
    if verbose:
        print('Loading particle data...')
    if use_fof:
        cat = il.snapshot.loadHalo(
            basedir, snapnum, object_id, parttype_stars, fields=fields)
    else:
        cat = il.snapshot.loadSubhalo(
            basedir, snapnum, object_id, parttype_stars, fields=fields)
    pos = cat['Coordinates']  # comoving kpc/h
    initial_masses = cat['GFM_InitialMass']
    metallicities = cat['GFM_Metallicity']
    formtimes = cat['GFM_StellarFormationTime']  # actually the scale factor

    # Remove wind particles
    locs_notwind = formtimes > 0
    pos = pos[locs_notwind]
    initial_masses = initial_masses[locs_notwind]
    metallicities = metallicities[locs_notwind]
    formtimes = formtimes[locs_notwind]

    # Prepare input for BC03 model
    initial_masses_Msun = initial_masses * 1e10 / h
    z_form = 1.0/formtimes - 1.0  # "formtimes" is actually the scale factor
    stellar_ages_yr = (acosmo.age(z) - acosmo.age(z_form)).value * 1e9

    # Get smoothing lengths in 3D (before making 2D projection)
    # once and for all, in simulation units [ckpc/h].
    # We temporarily set an arbitrary center (the position of the most bound
    # stellar particle) to account for the periodic boundary conditions.
    if verbose:
        print('Calculating smoothing lengths...')
    dx = pos[:] - pos[0]
    dx = dx - (np.abs(dx) > 0.5 * box_size) * np.copysign(box_size, dx)

    hsml_ckpc_h = get_hsml(dx, num_neighbors)

    # Get all fluxes once and for all.
    fluxes = np.empty((len(filter_names), len(initial_masses_Msun)), dtype=np.float64)
    for i, filter_name in enumerate(filter_names):
        fluxes[i, :] = get_fluxes(
            initial_masses_Msun, metallicities, stellar_ages_yr, filter_name)

    for subfind_id in fof_subfind_ids:
        create_image_single_sub(subfind_id, pos, hsml_ckpc_h, fluxes)

    print('Finished for object %d.\n' % (object_id,))


def master():
    """
    Master process (to be run by process with rank 0).
    """
    status = MPI.Status()
    
    # Initialize by sending one unit of work to each slave
    cur_pos = 0
    for k in range(1, size):
        object_id = object_ids[cur_pos]
        comm.send(obj=object_id, dest=k, tag=WORK_TAG)
        cur_pos += 1

    # While there is more work...
    while cur_pos < len(object_ids):
        object_id = object_ids[cur_pos]
        # Get results from slave
        comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)

        # Send another unit of work to slave
        comm.send(obj=object_id, dest=status.source, tag=WORK_TAG)
        cur_pos += 1

    # Get remaining results and kill slave processes
    for k in range(1, size):
        comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)

        # Send KILL_TAG
        comm.send(obj=None, dest=status.source, tag=KILL_TAG)


def slave():
    """
    Slave process.
    """
    status = MPI.Status()

    # Iterate until slave receives the KILL_TAG
    while True:
        object_id = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)

        if status.tag == KILL_TAG:
            return

        # Do the work
        create_images(object_id)

        # Let the master know that the job is done
        comm.send(obj=None, dest=0, tag=object_id)


if __name__ == '__main__':
    try:
        suite = sys.argv[1]
        simulation = sys.argv[2]
        basedir = sys.argv[3]
        amdir = sys.argv[4]  # can be set to a dummy value if not needed
        writedir = sys.argv[5]
        codedir = sys.argv[6]
        snapnum = int(sys.argv[7])
        use_z = float(sys.argv[8])  # if -1, use intrinsic snapshot redshift
        arcsec_per_pixel = float(sys.argv[9])
        proj_kind = sys.argv[10]  # 'xy', 'yz', 'zx', 'planar', 'faceon', 'edgeon'
        num_neighbors = int(sys.argv[11])  # for adaptive smoothing, usually 32
        num_rhalfs = float(sys.argv[12])  # measured from center, usually 7.5 (or -1)
        num_r200 = float(sys.argv[13])  # usually 1.0 for clusters (-1 if not used)
        npixels = int(sys.argv[14])  # total # of pixels on each side (-1 if not used)
        log_mstar_min = float(sys.argv[15])  # minimum log10(M*) (-1 if not used)
        log_m200_min = float(sys.argv[16])  # minimum log10(M200) (-1 if not used)
        filename_ids_custom = sys.argv[17]  # optional (-1 if not used)
        centrals_only = bool(int(sys.argv[18]))
        use_fof = bool(int(sys.argv[19]))  # If True, load particles from FoF group
        use_cf00 = bool(int(sys.argv[20]))  # If True, apply Charlot & Fall (2000)
        nprocesses = int(sys.argv[21])  # Use MPI if nprocesses > 1
        verbose = bool(int(sys.argv[22]))
    except:
        print('Arguments: suite simulation basedir amdir ' + 
              'writedir codedir snapnum use_z arcsec_per_pixel ' +
              'proj_kind num_neighbors num_rhalfs num_r200 npixels ' +
              'log_mstar_min log_m200_min filename_ids_custom ' +
              'centrals_only use_fof use_cf00 nprocesses verbose')
        sys.exit()

    # Check input
    if (num_rhalfs > 0) + (num_r200 > 0) + (npixels > 0) != 1:
        raise Exception(
            'One (and only one) of num_rhalfs, num_r200, and npixels ' +
            'should be defined (the others should be -1).')

    # Disable the option of creating images for satellite galaxies when
    # the field of view is determined by the halo radius (R200).
    if num_r200 > 0 and not centrals_only:
        raise Exception('Defining num_r200 only makes sense for centrals.')

    # Check if we require the angular momentum vectors
    require_jstar = proj_kind in ['planar', 'faceon', 'edgeon']

    # Some additional directories and filenames
    suitedir = '%s/%s' % (writedir, suite)
    simdir = '%s/%s' % (suitedir, simulation)
    filename_filters = '%s/filters.txt' % (writedir,)

    # Cosmology
    if suite == 'IllustrisTNG':  # Planck 2015 XIII (Table 4, last column)
        acosmo = FlatLambdaCDM(H0=67.74, Om0=0.3089, Ob0=0.0486)
    elif suite == 'Illustris':  # WMAP-7, Komatsu et al. 2011 (Table 1, v2)
        acosmo = FlatLambdaCDM(H0=70.4, Om0=0.2726, Ob0=0.0456)
    else:
        raise Exception("Cosmology not specified.")

    # MPI stuff (optional)
    comm, rank, size = None, 0, 1
    if nprocesses > 1:
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()
        size = comm.Get_size()

    # Save images here
    synthdir = '%s/snapnum_%03d/galaxev/%s' % (simdir, snapnum, proj_kind)
    if use_cf00:
        synthdir += '_cf00'
    datadir = '%s/data' % (synthdir,)

    # Create write directory if it does not exist
    if rank == 0:
        if not os.path.lexists(datadir):
            os.makedirs(datadir)
    if nprocesses > 1:
        comm.Barrier()

    # Read filter names
    with open(filename_filters, 'r') as f:
        filter_names = list(map(lambda s: s.strip('\n'), f.readlines()))
    num_filters = len(filter_names)

    # Load some info from snapshot header
    header = il.groupcat.loadHeader(basedir, snapnum)
    h = header['HubbleParam']
    z = header['Redshift']
    box_size = header['BoxSize']

    # If use_z is not specified, use the intrinsic snapshot redshift:
    if use_z == -1:
        use_z = z
        # However, if for some reason we are given the last snapshot,
        # set an arbitrary redshift:
        if ((suite == 'Illustris' and snapnum == 135) or (
             suite == 'IllustrisTNG' and snapnum == 99)):
            use_z = 0.0994018026302  # corresponds to snapnum_last - 8
            print('WARNING: use_z is too small. Setting use_z = %g.' % (use_z,))

    # Calculate spatial scale in the simulation (in comoving kpc/h)
    # that corresponds to a pixel:
    rad_per_pixel = arcsec_per_pixel / (3600.0 * 180.0 / np.pi)
    # Angular-diameter distance in comoving coordinates at z (ckpc/h):
    d_A_ckpc_h = acosmo.angular_diameter_distance(use_z).value * 1000.0 * h * (1.0 + z)
    ckpc_h_per_pixel = rad_per_pixel * d_A_ckpc_h

    # MPI parallelization
    if rank == 0:
        if verbose:
            print('snapnum = %d, use_z = %g' % (snapnum, use_z))
            print('arcsec_per_pixel = %g' % (arcsec_per_pixel,))
            print('ckpc_h_per_pixel = %g' % (ckpc_h_per_pixel,))

        # Load halo and subhalo info
        start = time.time()
        print('Loading halo and subhalo info...')
        sub_rhalf = il.groupcat.loadSubhalos(
            basedir, snapnum, fields=['SubhaloHalfmassRadType'])[:, parttype_stars]
        sub_pos = il.groupcat.loadSubhalos(basedir, snapnum, fields=['SubhaloPos'])
        sub_gr_nr = il.groupcat.loadSubhalos(basedir, snapnum, fields=['SubhaloGrNr'])
        # Load angular momentum vectors if necessary
        if require_jstar:
            with h5py.File('%s/jstar_%03d.hdf5' % (amdir, snapnum), 'r') as f:
                jstar_direction = f['jstar_direction'][:]
        else:
            jstar_direction = None
        # Load halo radius (R200) if necessary
        if num_r200 > 0:
            group_r200 = il.groupcat.loadHalos(
                basedir, snapnum, fields=['Group_R_Crit200'])
        else:
            group_r200 = None
        print('Time: %f s.' % (time.time() - start))

        # Get list of relevant Subfind IDs
        filename_ids = '%s/subfind_ids.txt' % (synthdir,)
        if log_mstar_min == -1 and log_m200_min == -1:
            subfind_ids = np.loadtxt(filename_ids_custom, dtype=np.int32)
        else:
            subfind_ids = get_subfind_ids(snapnum)
        # Write Subfind IDs to text file
        with open(filename_ids, 'w') as f_ids:
            for sub_index in subfind_ids:
                f_ids.write('%d\n' % (sub_index,))

        # Get associated FoF group IDs
        fof_ids = sub_gr_nr[subfind_ids]

    else:
        sub_rhalf = None
        sub_pos = None
        sub_gr_nr = None
        jstar_direction = None
        group_r200 = None
        subfind_ids = None
        fof_ids = None

    # For simplicity, all processes will have a copy of these arrays:
    if nprocesses > 1:
        comm.Barrier()
        sub_rhalf = comm.bcast(sub_rhalf, root=0)
        sub_pos = comm.bcast(sub_pos, root=0)
        sub_gr_nr = comm.bcast(sub_gr_nr, root=0)
        jstar_direction = comm.bcast(jstar_direction, root=0)
        group_r200 = comm.bcast(group_r200, root=0)
        subfind_ids = comm.bcast(subfind_ids, root=0)
        fof_ids = comm.bcast(fof_ids, root=0)

    # Check that there are at least as many galaxies as (slave) processes
    # doing the work.
    if len(subfind_ids) < nprocesses - 1:
        raise Exception("Too many processes for too few galaxies. " +
                        "Should use at most %d." % (len(subfind_ids) + 1,))

    if rank == 0:
        # For performance checks
        start_all = time.time()

        # Create list of "generic" objects (halo or subhalo)
        if use_fof:
            object_ids = np.unique(fof_ids)
        else:
            object_ids = subfind_ids

        # Create images
        if nprocesses > 1:
            start_time = MPI.Wtime()
            master()
            end_time = MPI.Wtime()
            print("MPI Wtime: %f s.\n" % (end_time - start_time))
        else:  # no MPI
            for object_id in object_ids:
                create_images(object_id)
        print('Total time: %f s.\n' % (time.time() - start_all))

    else:
        slave()
