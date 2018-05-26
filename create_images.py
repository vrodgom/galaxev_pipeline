import numpy as np
import h5py
import os
import sys
import scipy.interpolate as ip
from scipy.spatial import cKDTree
from astropy.io import fits
from multiprocessing import Pool
import time
import ctypes

import illustris_python as il
import cosmology as cosmo

parttype_stars = 4

def get_fluxes(initial_masses_Msun, metallicities, stellar_ages_yr, filter_name):
    """
    Return flux in photons/s (Pan-STARRS) or in photons/s/m^2 (SDSS, etc.)
    using BC03 table generated by stellar_photometrics.py.
    """
    if use_cf00:
        filename = '%s/stellar_photometrics_%03d_cf00.hdf5' % (stellar_photometrics_dir, snapnum)
    else:
        filename = '%s/stellar_photometrics_%03d.hdf5' % (stellar_photometrics_dir, snapnum)

    with h5py.File(filename, 'r') as f:
        bc03_metallicities = f['metallicities'][:]
        bc03_stellar_ages = f['stellar_ages'][:]
        bc03_magnitudes = f[filter_name][:]
        zeropoint_phot = f[filter_name].attrs['zeropoint_phot']

    spline = ip.RectBivariateSpline(
        bc03_metallicities, bc03_stellar_ages, bc03_magnitudes, kx=1, ky=1, s=0)

    magnitudes = spline.ev(metallicities, stellar_ages_yr) - 2.5 * np.log10(initial_masses_Msun)

    # Convert apparent magnitudes to photon fluxes.
    # Depending on the units of the filter curves, the fluxes might be in
    # photons/s (Pan-STARRS) or photons/s/m^2 (SDSS). We assume that we're
    # dealing with Pan-STARRS.
    fluxes = 10.0**(-2.0/5.0*magnitudes) * zeropoint_phot
    
    return fluxes


def transform(x, jvec, proj_kind='xy'):
    """
    Return a projection of the particle positions. In all cases we only
    return the first two coordinates (x, y).

    Parameters
    ----------
    x : array-like
        2-dimensional array (Nx3) with the particle positions.
    jvec : array-like
        Direction of the galaxy's total stellar angular momentum.
    proj_kind : str, optional
        Specify which kind of projection. The possible values are:

        'yz' : Project onto the yz plane.
        'zx' : Project onto the zx plane.
        'xy' : Project onto the xy plane.
        'planar' : Rotate around z axis until jvec is in the yz plane. Return xy.
        'edgeon' : jvec perpendicular to z-axis.
        'faceon' : jvec parallel to z-axis.
        
    Returns
    -------
    x_new : array-like
        2-dimensional array (Nx2) with the projected particle positions.

    """
    assert len(jvec) == x.shape[1] == 3

    # Define unit basis vectors of new reference frame
    if proj_kind == 'yz':
        e1 = np.array([0,1,0])
        e2 = np.array([0,0,1])
    elif proj_kind == 'zx':
        e1 = np.array([0,0,1])
        e2 = np.array([1,0,0])
    elif proj_kind == 'xy':
        e1 = np.array([1,0,0])
        e2 = np.array([0,1,0])
    elif proj_kind == 'planar':
        # New y-axis is the projection of jvec onto the xy plane
        e2 = np.array([jvec[0], jvec[1], 0.0])
        e2 = e2 / np.linalg.norm(e2)  # normalize
        e1 = np.cross(e2, np.array([0,0,1]))
    elif proj_kind == 'edgeon':
        # New y-axis is aligned with jvec
        e2 = jvec[:] / np.linalg.norm(jvec)  # normalize
        e1 = np.cross(e2, np.array([0,0,1]))
    elif proj_kind == 'faceon':
        # New z-axis is aligned with jvec
        e3 = jvec[:] / np.linalg.norm(jvec)  # normalize
        # New x-axis is chosen to coincide with edge-on projection
        e1 = np.cross(e3, np.array([0,0,1]))
        e2 = np.cross(e3, e1)
    else:
        raise Exception('Projection kind not understood.')

    # Project onto new axes
    x_new = np.zeros((x.shape[0], 2), dtype=np.float64)
    x_new[:,0] = np.dot(x, e1)
    x_new[:,1] = np.dot(x, e2)
    
    return x_new

def get_hsml(x, y, z, num_neighbors=16):
    """
    Get distance to the Nth (usually 16th) nearest neighbor in 3D.
    
    Parameters
    ----------
    x : array-like
        x-coordinates of the particles.
    y : array-like
        y-coordinates of the particles.
    z : array-like
        z-coordinates of the particles.
    num_neighbors : int, optional
        Specifies how many neighbors to search for.

    Returns
    -------
    hsml : array-like
        Distances to the Nth nearest neighbors.
    
    """
    data = np.empty((len(x), 3))
    data[:,0] = x.ravel()
    data[:,1] = y.ravel()
    data[:,2] = z.ravel()

    tree = cKDTree(data)
    res = tree.query(data, k=num_neighbors+1)
    hsml = res[0][:,-1]

    return hsml

def adaptive_smoothing(x, y, hsml, xcenters, ycenters, weights=None):
    """
    Do adaptive smoothing similar to Torrey et al. (2015).

    Parameters
    ----------
    x : array-like
        x-coordinates of the particles.
    y : array-like
        y-coordinates of the particles.
    hsml : array-like
        Distances to the Nth nearest neighbors *or* desired
        adaptive smoothing lengths.
    xcenters : array-like
        1-d array with the pixel centers along the x-axis
    ycenters : array-like
        1-d array with the pixel centers along the y-axis
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

    start = time.time()
    print('Doing adaptive smoothing...')

    X, Y = np.meshgrid(xcenters, ycenters)
    ny, nx = X.shape
    Y_flat, X_flat = Y.ravel(), X.ravel()
    Z_flat = np.zeros_like(X_flat)
    sphlib.add(
        X_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        Y_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        Z_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.c_int(X.shape[1]),
        ctypes.c_int(X.shape[0]),
        x.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        y.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        weights.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        hsml.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        ctypes.c_int(x.size),
        ctypes.c_double(num_rhalfs))

    H = Z_flat.reshape(X.shape)

    print('Time: %g s.' % (time.time() - start))

    return H

def get_subfind_ids(snapnum, log_mstar_bin_lower, log_mstar_bin_upper, mstar):
    nsubs = len(mstar)

    # Mass bins
    mstar_bin_lower = 10.0**log_mstar_bin_lower / 1e10 * h
    mstar_bin_upper = 10.0**log_mstar_bin_upper / 1e10 * h
    num_mstar_bins = len(log_mstar_bin_lower)

    # Iterate over mass bins
    subfind_ids = []
    for mstar_bin_index in range(num_mstar_bins):
        mstar_min = 10.0**log_mstar_bin_lower[mstar_bin_index] / 1e10 * h
        mstar_max = 10.0**log_mstar_bin_upper[mstar_bin_index] / 1e10 * h

        # Only proceed if there are enough galaxies
        locs_valid = ((mstar >= mstar_min) * (mstar < mstar_max))
        if np.sum(locs_valid) == 0:
            print('Not enough galaxies. Skipping...')
            return

        # Iterate over subhalos
        for subfind_id in range(nsubs):
            # Only proceed if current subhalo is within mass range
            if locs_valid[subfind_id]:
                subfind_ids.append(subfind_id)

    return subfind_ids

def create_file(subfind_id):
    """
    Create (adaptively smoothed) images for the chosen filters.
    """
    # Define 2D bins (in units of rhalf)
    npixels = int(np.ceil(2.0*num_rhalfs*rhalf[subfind_id]/kpc_h_per_pixel))
    print('npixels = %d' % (npixels))
    xedges = np.linspace(-num_rhalfs, num_rhalfs, num=npixels+1)
    yedges = np.linspace(-num_rhalfs, num_rhalfs, num=npixels+1)
    xcenters = 0.5 * (xedges[:-1] + xedges[1:])
    ycenters = 0.5 * (yedges[:-1] + yedges[1:])

    # Load stellar particle info
    start = time.time()
    print('Loading info from snapshot...')
    cat_sub = il.snapshot.loadSubhalo(
        basedir, snapnum, subfind_id, parttype_stars,
        fields=['Coordinates', 'GFM_InitialMass', 'GFM_Metallicity', 'GFM_StellarFormationTime'])
    all_pos = cat_sub['Coordinates']  # comoving kpc/h
    all_initial_masses = cat_sub['GFM_InitialMass']
    all_metallicities = cat_sub['GFM_Metallicity']
    all_formtimes = cat_sub['GFM_StellarFormationTime']  # actually the scale factor
    print('Time: %f s.' % (time.time() - start))

    # Remove wind particles
    locs_notwind = all_formtimes >= 0
    pos = all_pos[locs_notwind]
    initial_masses = all_initial_masses[locs_notwind]
    metallicities = all_metallicities[locs_notwind]
    formtimes = all_formtimes[locs_notwind]

    # Prepare input for BC03 model
    initial_masses_Msun = initial_masses * 1e10 / h
    z_form = 1.0/formtimes - 1.0
    params = cosmo.CosmologicalParameters(suite=suite)
    stellar_ages_yr = (cosmo.t_Gyr(z, params) - cosmo.t_Gyr(z_form, params)) * 1e9

    # Periodic boundary conditions
    dx = pos[:] - pos[0]
    dx = dx - (np.abs(dx) > 0.5*box_size) * np.copysign(box_size, dx - 0.5*box_size)

    # Normalize by rhalf
    dx = dx / rhalf[subfind_id]  # in rhalfs

    # Get smoothing lengths in 3D (before making 2D projection)
    start = time.time()
    print('Doing spatial search...')
    hsml = get_hsml(dx[:,0], dx[:,1], dx[:,2])  # in rhalfs
    print('Time: %g s.' % (time.time() - start))

    # Transform particle positions according to 'proj_kind' (2D projection)
    dx_new = transform(dx, jstar_direction[subfind_id], proj_kind=proj_kind)

    # Impose minimum smoothing length equal to 2.8 times the (Plummer-equivalent)
    # gravitational softening length:
    softening_length_rhalfs = softening_length / rhalf[subfind_id]
    eps_to_hsml = 2.8
    hsml = np.where(hsml >= eps_to_hsml * softening_length_rhalfs, hsml,
                    eps_to_hsml * softening_length_rhalfs)

    # Store images here
    image = np.zeros((num_filters,npixels,npixels), dtype=np.float32)

    # Iterate over broadband filters
    for i, filter_name in enumerate(filter_names):
        fluxes = get_fluxes(
            initial_masses_Msun, metallicities, stellar_ages_yr, filter_name)
        H = adaptive_smoothing(
            dx_new[:,0], dx_new[:,1], hsml, xcenters, ycenters, weights=fluxes)
        # Store in array
        image[i,:,:] = H

    # ~ # Number of particles per pixel
    # ~ counts_map = adaptive_smoothing(dx_new[:,0], dx_new[:,1], hsml, xcenters, ycenters,
                                    # ~ weights=None)

    # Convert area units from rhalf^{-2} to pixel_size^{-2}
    pixel_size_rhalfs =  2.0 * num_rhalfs / float(npixels)  # in rhalfs
    image *= pixel_size_rhalfs**2
    # ~ counts_map *= pixel_size_rhalfs**2

    # Create some header attributes
    header = fits.Header()
    header["BUNIT"] = ("counts/s/pixel", "Unit of the array values")
    header["CDELT1"] = (kpc_h_per_pixel / h / (1.0 + z), "Coordinate increment along X-axis")
    header["CTYPE1"] = ("kpc", "Physical units of the X-axis increment")
    header["CDELT2"] = (kpc_h_per_pixel / h / (1.0 + z), "Coordinate increment along Y-axis")
    header["CTYPE2"] = ("kpc", "Physical units of the Y-axis increment")
    header["PIXSCALE"] = (arcsec_per_pixel, "Pixel size in arcsec")
    header["Z"] = (z, "Redshift of the source")
    for k in range(num_filters):
        header["FILTER%d" % (k)] = (filter_names[k], "Broadband filter index = %d" % (k))

    # Write to FITS file
    hdu = fits.PrimaryHDU(data=image, header=header)
    hdulist = fits.HDUList([hdu])
    hdulist.writeto('%s/broadband_%d.fits' % (datadir, subfind_id))

    print('Finished for subhalo %d.\n' % (subfind_id))


if __name__ == '__main__':
    try:
        suite = sys.argv[1]
        basedir = sys.argv[2]
        amdir = sys.argv[3]
        filename_filters = sys.argv[4]
        stellar_photometrics_dir = sys.argv[5]
        writedir = sys.argv[6]
        codedir = sys.argv[7]
        snapnum = int(sys.argv[8])
        proj_kind = sys.argv[9]  # 'yz', 'zx', 'xy', 'planar', 'faceon', 'edgeon'
        use_cf00 = bool(int(sys.argv[10]))
        nprocesses = int(sys.argv[11])
    except:
        print('Arguments: suite basedir amdir filename_filters ' + 
              'stellar_photometrics_dir writedir codedir snapnum ' +
              'proj_kind use_cf00 nprocesses')
        sys.exit()

    max_softening_length = 0.5  # kpc/h
    num_rhalfs = 10.0  # on each side from the center
    num_neighbors = 16  # for adaptive smoothing
    arcsec_per_pixel = 0.258  # Pan-STARRS PS1

    # Save images here
    if use_cf00:
        synthdir = '%s/snapnum_%03d/%s_cf00' % (writedir, snapnum, proj_kind)
    else:
        synthdir = '%s/snapnum_%03d/%s' % (writedir, snapnum, proj_kind)
    datadir = '%s/data' % (synthdir)
    if not os.path.lexists(datadir):
        os.makedirs(datadir)

    # Read filter names
    with open(filename_filters, 'r') as f:
        filter_names = list(map(lambda s: s.strip('\n'), f.readlines()))
    num_filters = len(filter_names)

    # Load some info from snapshot header
    with h5py.File('%s/snapdir_%03d/snap_%03d.0.hdf5' % (basedir, snapnum, snapnum), 'r') as f:
        header = dict(f['Header'].attrs.items())
        h = header['HubbleParam']
        z = header['Redshift']
        box_size = header['BoxSize']

    # Try sigma equal to softening length at current redshift
    if z < 1:
        softening_length = (1.0 + z) * max_softening_length  # comoving kpc/h
    else:
        softening_length = 2.0 * max_softening_length  # comoving kpc/h

    # Get angular-diameter distance (if redshift is too small, assume source is at 10 Mpc)
    if z < 2.5e-3:
        d_A_kpc_h = 10000.0 * h
        print('WARNING: assuming that source is at 10 Mpc.')
    else:
        params = cosmo.CosmologicalParameters(suite=suite)
        d_A_kpc_h = cosmo.angular_diameter_distance_Mpc(z, params) * 1000.0 * h  # physical kpc/h

    # Get pixel scale at redshift of interest
    rad_per_pixel = arcsec_per_pixel / (3600.0 * 180.0 / np.pi)
    kpc_h_per_pixel = rad_per_pixel * d_A_kpc_h * (1.0+z)  # comoving kpc/h
    print('rad_per_pixel =', rad_per_pixel)
    print('d_A_kpc_h =', d_A_kpc_h)
    print('kpc_h_per_pixel =', kpc_h_per_pixel)

    # Load subhalo info
    start = time.time()
    print('Loading subhalo info...')
    mstar = il.groupcat.loadSubhalos(basedir, snapnum, fields=['SubhaloMassType'])[:, parttype_stars]
    rhalf = il.groupcat.loadSubhalos(basedir, snapnum, fields=['SubhaloHalfmassRadType'])[:, parttype_stars]
    with h5py.File('%s/jstar_%03d.hdf5' % (amdir, snapnum), 'r') as f:
        jstar_direction = f['jstar_direction'][:]
    nsubs = len(mstar)
    print('Time: %f s.' % (time.time() - start))

    # For performance checks
    start_all = time.time()

    # Define stellar mass bins
    log_mstar_bin_lower = np.array([9.5])
    log_mstar_bin_upper = np.array([13.0])
    mstar_bin_lower = 10.0**log_mstar_bin_lower / 1e10 * h
    mstar_bin_upper = 10.0**log_mstar_bin_upper / 1e10 * h

    # Get list of relevant Subfind IDs
    subfind_ids = get_subfind_ids(snapnum, log_mstar_bin_lower, log_mstar_bin_upper, mstar)

    if nprocesses == 1:
        for subfind_id in subfind_ids:
            create_file(subfind_id)
    else:
        p = Pool(nprocesses)
        p.map(create_file, subfind_ids)

    print('Total time: %f s.\n' % (time.time() - start_all))