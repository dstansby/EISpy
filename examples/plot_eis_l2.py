import matplotlib.pyplot as plt
from matplotlib import gridspec
import numpy as np
import os

from eispy.cube import read

fname = 'eis_l2_20130116_093720.fits'
remote_dir = 'http://solar.ads.rl.ac.uk/MSSL-data/eis/level2/2013/01/16/'
if not os.path.exists(fname) and not os.path.exists(f'{fname}.gz'):
    import urllib.request
    print('Downloading example file...')
    filename, headers = urllib.request.urlretrieve(f'{remote_dir}{fname}.gz', f'{fname}.gz')
    print('Example file downloaded!')

if not os.path.exists(fname):
    import gzip
    with gzip.open(f'{fname}.gz', 'rb') as f:
        with open(fname, 'wb') as g:
            g.write(f.read())

eis_obs = read(fname)
print(eis_obs.wavelengths)

print(len(eis_obs.wavelengths))

fig = plt.figure(figsize=(12, 12))
spec = gridspec.GridSpec(ncols=4, nrows=3, figure=fig)
for i, wlen in enumerate(eis_obs.wavelengths):
    cube = eis_obs[wlen]
    ax = fig.add_subplot(spec[i % 3, i // 3], projection=cube.wcs.dropaxis(2))
    ax = cube[11, :, :].plot()
    # Fix aspect and axes limits
    cdelt = cube.wcs.wcs.cdelt
    ax.set_aspect(np.abs(cdelt[1] / cdelt[0]))
    xlim = ax.get_xlim()
    ax.set_xlim(xlim[1], xlim[0])
    # Set axis labels
    ax.set_xlabel('')
    ax.set_ylabel('')
    ax.set_title(wlen)
    ax.set_axis_off()

fig.subplots_adjust(top=0.95, bottom=0.05, left=0.05, right=0.95)
plt.show()
