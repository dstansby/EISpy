# -*- coding: utf-8 -*-
# Author: Mateo Inchaurrandieta <mateo.inchaurrandieta@gmail.com>
# pylint: disable=E1101
"""
Calibration and error calculation for EIS level 0 files.
This module calls several corrections, then attempts to interpolate missing or
damaged data, and calculates the 1-sigma errors of the good data.
"""

from __future__ import absolute_import
from astropy.io import fits
from scipy.io import readsav
import numpy as np
import datetime as dt
import locale
import urllib
from bs4 import BeautifulSoup
from astroscrappy import detect_cosmics

__missing__ = -100
__darts__ = "http://darts.jaxa.jp/pub/ssw/solarb/eis/data/cal/"
__pix_memo__ = {}


# /===========================================================================\
# |                          Methods for main steps                           |
# \===========================================================================/
def _read_fits(filename, **kwargs):
    """
    Reads a FITS file and returns two things: a dictionary of wavelengths to a
    3-tuple of 3D ndarrays (data, error, index) and a dictionary containing all
    the l0 metadata found in the file. The error array is initailly set to 0,
    and has the same shape as the data array. Extra keyword arguments are
    passed on to the astropy fits reader.

    Parameters
    ----------
    filename: str
        Location of the file to be opened.
    """
    hdulist = fits.open(filename, **kwargs)
    header = dict(hdulist[1].header)
    wavelengths = [c.name for c in hdulist[1].columns if c.dim is not None]
    data = {wav: hdulist[1].data[wav] for wav in wavelengths}
    data_with_errors = {k: (data[k], np.zeros(data[k].shape),
                            wavelengths.index(k) + 1) for k in data}
    return data_with_errors, header


def _remove_zeros_saturated(*data_and_errors):
    """
    Finds pixels in the data where the data numbers are zero or saturated,
    sets them to zero and marks them as bad in the error array. Note that this
    method modifies arrays in-place, and does not create or return new arrays.

    Parameters
    ----------
    data_and_errors: one or more 3-tuples of ndarrays
        tuples of the form (data, error, index) to be stripped of invalid data
    """
    for data, err, _ in data_and_errors:
        zeros = data <= 0
        saturated = data >= 16383  # saturated pixels have a value of 16383.
        zeros[saturated] = True  # equivalent to |=
        data[zeros] = 0
        err[zeros] = __missing__


def _remove_dark_current(meta, *data_and_errors, **kwargs):
    # TODO: support 40" slot
    # TODO: support dcfiles
    """
    Calculates and subtracts dark current and CCD pedestal from the data. If
    the retain keyword is set to True then values less than zero are kept,
    otherwise they are floored at zero and marked as missing.

    Parameters
    ----------
    meta: dict
        observation metadata
    data_and_errors: one or more 3-tuples of ndarrays
        tuples of the form (data, error, index) to be corrected
    retain: bool
        If True, data less than zero will be retained.
    """
    retain = kwargs.pop('retain', False)
    for data, err, idx in data_and_errors:
        print idx
        ccd_xwidth = meta['TDETXW' + str(idx)]
        if ccd_xwidth == 1024:
            _remove_dark_current_full_ccd(data, meta, idx)
        else:
            _remove_dark_current_part_ccd(data)
        if not retain:
            negatives = data <= 0
            data[negatives] = 0
            err[negatives] = __missing__


def _calibrate_pixels(meta, *data_and_errors):
    """
    Fetches and marks as missing hot, warm and dusty pixels present in the
    observation. If there is no data available for the exact date of a
    particular observation, the closest available one is used.

    Parameters
    ----------
    meta: dict
        observation metadata
    data_and_errors: one or more 3-tuples of ndarrays
        tuples of the form (data, error, index) to be corrected
    """
    for _, err, index in data_and_errors:
        date = dt.datetime.strptime(meta['DATE_OBS'][:10], "%Y-%m-%d")
        y_window = (meta['YWS'], meta['YWS'] + meta['YW'])
        x_window = (meta['TDETX' + str(index)], (meta['TDETX' + str(index)] +
                                                 meta['TDETXW' + str(index)]))
        detector = meta['TWBND' + str(index)].lower()
        hots = _get_pixel_map(date, 'hp', detector, y_window, x_window)
        warms = _get_pixel_map(date, 'wp', detector, y_window, x_window)
        dusties = _get_dusty_array(y_window, x_window)
        locations = hots == 1
        locations[warms == 1] = True
        locations[dusties == 1] = True
        for x_slice in err:
            x_slice[locations] = __missing__


def _remove_cosmic_rays(*data_and_errors, **kwargs):
    """
    Removes and corrects for cosmic ray damage in the measurements. This method
    uses astroscrappy, so refer to that documentation for fine-tuning the
    keyword arguments.

    Parameters
    ----------
    data_and_errors: one or more 3-tuples of ndarrays
        tuples of the form (data, error, index) to be corrected
    kwargs: dict-like, optional
        Extra arguments to be passed on to astroscrappy.
    """
    for data, err, _ in data_and_errors:
        slices = [detect_cosmics(ccd_slice, **kwargs) for ccd_slice in data]
        data = np.array([ccd_slice[1] for ccd_slice in slices])
        mask = np.array([ccd_slice[0] for ccd_slice in slices])
        err[mask] = True


# /===========================================================================\
# |                              Utility methods                              |
# \===========================================================================/

# ==========================    Dark current utils    =========================
def _remove_dark_current_full_ccd(data, meta, window):
    """
    Remove the dark current and CCD pedestal from a data array that encompasses
    the entire CCD.

    Parameters
    ----------
    data: 3D numpy array
        The CCD data
    meta: dict
        observation metadata
    window: int
        data window from FITS file
    """
    ccd_x_start = meta['TDETX' + str(window)]
    if ccd_x_start == 1024:
        pixels = (944, 989)
    elif ccd_x_start == 3072:
        pixels = (926, 971)
    else:
        pixels = (39, 84)
    quiet_vals = data[:, :, pixels[0]:pixels[1]]
    data -= np.median(quiet_vals)


def _remove_dark_current_part_ccd(data):
    """
    Remove the dark current and CCD pedestal from a data array that takes up
    only part of the CCD

    Parameters
    ----------
    data: 3D numpy array
        The CCD data
    """
    flatarr = data.flatten()
    flatarr.sort()
    low_value = np.median(flatarr[:0.02 * flatarr.shape[0]])
    data -= low_value


# =======================    Pixel calibration utils    =======================
def _download_calibration_data(date, pix_type, detector, top_bot, left_right):
    """
    Downloads the requested calibration data from the DARTS repository. If the
    required data is not present, it looks for the nearest one that fits the
    requirements.

    Parameters
    ----------
    date: datetime object
        Date of the observation.
    pix_type: 'dp', 'hp', 'wp'
        Whether to download data for dusty, hot or warm pixels.
    detector: 'a', 'b'
        Long- or Short-wave detector
    top_bot: 'top', 'bottom', 'middle', 'both'
        Y location of the observation on the CCD
    left_right: 'left', 'right', 'both'
        X location of the observation on the CCD (left, right, or both)
    """
    tb_tuple = ('top', 'bottom') if top_bot == 'both' else (top_bot,)
    lr_tuple = ('left', 'right') if left_right == 'both' else (left_right,)
    retfiles = {}
    for vert in tb_tuple:
        for horiz in lr_tuple:
            key = vert + horiz
            arr = _try_download_nearest_cal(date, pix_type, detector, vert,
                                            horiz)
            retfiles.update({key: arr})
    return retfiles


def _get_dusty_array(y_window, x_window):
    """
    Returns the sliced array of dusty pixels
    """
    url = __darts__ + 'dp/dusty_pixels.sav'
    http_down = urllib.urlretrieve(url)
    dusties = readsav(http_down[0]).dp_data
    return dusties[y_window[0]:y_window[1], x_window[0]: x_window[1]]


def _try_download_nearest_cal(date, pix_type, detector, top_bot, left_right):
    """
    Tries to download the requested calibration data, looking for up to one
    month before and after to do so.
    """
    key = _construct_hot_warm_pix_url(date, pix_type, detector, top_bot,
                                      left_right)
    if key in __pix_memo__:
        return __pix_memo__[key]
    dates = _get_cal_dates(pix_type)
    dates.sort(key=lambda d: d - date if d > date else date - d)
    # dates is now a sorted list of the dates closest to the specified date
    for cal_date in dates:
        url = _construct_hot_warm_pix_url(cal_date, pix_type, detector,
                                          top_bot, left_right)
        http_response = urllib.urlopen(url)
        http_response.close()
        if http_response.code == 200:  # HTTP OK
            http_down = urllib.urlretrieve(url)
            arr = readsav(http_down[0]).ccd_data
            __pix_memo__[key] = arr
            return arr


def _get_cal_dates(pix_type):
    """
    Retrieves the list of available dates for a given pixel type.
    """
    url = __darts__ + pix_type + '/'
    http_request = urllib.urlopen(url)
    soup = BeautifulSoup(http_request.read())
    http_request.close()
    links = soup.find_all('a')
    date_str = [link.get('href') for link in links]
    dates = [dt.datetime.strptime(date, '%Y-%m-%d/') for date in date_str[5:]]
    return dates  # This isn't a numpy array so we can sort by keys.


def _construct_hot_warm_pix_url(date, pix_type, detector, top_bot, left_right):
    """
    Constructs a DARTS URL to download hot or warm pixels given the relevant
    parameters.
    """
    url = __darts__
    url += pix_type + '/'
    url += date.strftime("%Y-%m-%d") + '/'
    url += 'coords_' + detector + '_' + left_right + '_'
    loc = locale.getlocale()
    locale.setlocale(locale.LC_ALL, 'en_GB')
    datestr = date.strftime("%d%b%y").lower()
    locale.setlocale(locale.LC_ALL, loc[0])
    if pix_type == 'wp':
        url += top_bot + '_'
        url += datestr + '_100s.sav'
    else:
        url += datestr
        if detector != 'middle':
            url += '_' + top_bot
        url += '.sav'
    return url


def _calculate_detectors(date, y_window_start, n_y_pixels, x_start, x_width):
    """
    Calculates what area of the detector the observation is in.
    """
    if date <= dt.datetime(2008, 1, 18):
        top_bot = 'middle'
    else:
        top_bot = 'top' if y_window_start >= 512 else \
                  'bottom' if y_window_start + n_y_pixels <= 512 else \
                  'both'
    # XXX: Warning: there may be an off-by-one error here!
    left_right = 'right' if x_start >= 1024 else \
                 'left' if (x_start + x_width) < 1024 else \
                 'both'
    return top_bot, left_right


def _get_pixel_map(date, pix_type, detector, y_window, x_window):
    """
    Returns the pixel calibration map for the specified pixel type and detector
    at the given date.
    """
    y_window_start = y_window[0]
    x_start = x_window[0] % 2048
    n_y_pixels = y_window[1] - y_window[0]
    x_width = x_window[1] - x_window[0]
    detector_areas = _calculate_detectors(date, y_window_start, n_y_pixels,
                                          x_start, x_width)
    arrays = _download_calibration_data(date, pix_type, detector,
                                        detector_areas[0], detector_areas[1])
    glued_array = np.zeros((1024, 2048))
    zero_arr = np.zeros((512, 1024))
    glued_array[0:512, 0:1024] = arrays.get('topleft', zero_arr)
    glued_array[512:1024, 1024:2048] = arrays.get('bottomright', zero_arr)
    glued_array[512:1024, 0:1024] = arrays.get('bottomleft', zero_arr)
    glued_array[0:512, 1024:2048] = arrays.get('topright', zero_arr)
    return glued_array[y_window_start:y_window[1],
                       x_start:(x_window[1] % 2048)]