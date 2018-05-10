#include <math.h>

// Compile as:
// gcc -o adaptive_smoothing.so -shared -fPIC -O3 adaptive_smoothing.c

double sph_kernel(const double r, const double h) {
    // Kernel from Monaghan (1992) for 2 dimensions, defined over
    // the interval [0, h] as in Springel (2001), eq. (A.1)
    static const double pi = 3.141592653589793;
    double x = r / h;
    double retval;
    if (x <= 0.5)  // but hopefully x >= 0
        retval = 1.0 - 6.0*x*x + 6.0*x*x*x;
    else if (x <= 1)
        retval = 2.0 * (1.0 - x) * (1.0 - x) * (1.0 - x);
    else  // x > 1
        retval = 0.0;
    return 4.0 * 10.0 / (7.0*pi*h*h) * retval;

}

int rhalfs_to_pixels(const double r, const int npix, const double num_rhalfs) {
    return (int) floor(npix/2.0 + r*npix/(2.0*num_rhalfs));
}

void add(const double *X, const double *Y, double *Z, const int nx, const int ny,
         const double* x0, const double* y0, const double* weights, const double* hsml,
         const int npoints, const double num_rhalfs) {
    // Z should already be initialized to zero
    static const double num_hsml = 2.8;
    int i, j, k;
    for (k = 0; k < npoints; k++) {
        double h = num_hsml*hsml[k];
        int imin = (int) fmax(0,    rhalfs_to_pixels(y0[k]-h, ny, num_rhalfs));
        int imax = (int) fmin(ny-1, rhalfs_to_pixels(y0[k]+h, ny, num_rhalfs));
        int jmin = (int) fmax(0,    rhalfs_to_pixels(x0[k]-h, nx, num_rhalfs));
        int jmax = (int) fmin(nx-1, rhalfs_to_pixels(x0[k]+h, nx, num_rhalfs));
        for (i = imin; i < imax+1; i++) {
            for (j = jmin; j < jmax+1; j++) {
                int n = i*ny + j;
                double r = sqrt((X[n]-x0[k])*(X[n]-x0[k]) + (Y[n]-y0[k])*(Y[n]-y0[k]));
                Z[n] += weights[k] * sph_kernel(r, h);
            }
        }
    }
}
