# -*- coding: utf-8 -*-

import numpy as np
import numba
from numba.experimental import jitclass
import numba.types

from africanus.util.numba import generated_jit, njit
from africanus.averaging.support import unique_time, unique_baselines


bin_spw_dt = numba.int16


class RowMapperError(Exception):
    pass


_SERIES_COEFFS = (1./40, 107./67200, 3197./24192000, 49513./3973939200)


@njit(nogil=True, cache=True, inline='always')
def inv_sinc(sinc_x, tol=1e-12):
    # Initial guess from reversion of Taylor series
    # https://math.stackexchange.com/questions/3189307/inverse-of-frac-sinxx
    x = t_pow = np.sqrt(6*(1 - sinc_x))
    t_squared = t_pow*t_pow

    for coeff in _SERIES_COEFFS:
        t_pow *= t_squared
        x += coeff * t_pow

    # Use Newton Raphson to go the rest of the way
    # https://www.wolframalpha.com/input/?i=simplify+%28sinc%5Bx%5D+-+c%29+%2F+D%5Bsinc%5Bx%5D%2Cx%5D
    while True:
        # evaluate delta between this iteration sinc(x) and original
        sinx = np.sin(x)
        𝞓sinc_x = (1.0 if x == 0.0 else sinx/x) - sinc_x

        # Stop if converged
        if np.abs(𝞓sinc_x) < tol:
            break

        # Next iteration
        x -= (x*x * 𝞓sinc_x) / (x*np.cos(x) - sinx)

    return x


@njit
def partition_frequency(spws, chan_freq, chan_width, ref_freq,
                        max_uvw_dist, decorrelation):

    # Reversed as this produces monotically increasing
    # fractional bandwidth
    uvw_dists = np.linspace(max_uvw_dist, 1e-12, spws)
    𝞓𝞍 = inv_sinc(decorrelation)

    bandwidth = chan_width.sum()
    spw_chan_widths = []
    spw_chan_freqs = []

    for s in range(uvw_dists.shape[0]):
        fractional_bandwidth = 𝞓𝞍 / uvw_dists[s]

        # Derive max_𝞓𝝼, the maximum change in bandwidth
        # before decorrelation occurs in frequency
        #
        # Fractional Bandwidth is defined by
        # https://en.wikipedia.org/wiki/Bandwidth_(signal_processing)
        # for Wideband Antennas as:
        #   (1) 𝞓𝝼/𝝼 = fb = (fh - fl) / (fh + fl)
        # where fh and fl are the high and low frequencies
        # of the band.
        # We set fh = ref_freq + 𝞓𝝼/2, fl = ref_freq - 𝞓𝝼/2
        # Then, simplifying (1), 𝞓𝝼 = 2 * ref_freq * fb
        max_𝞓𝝼 = 2 * ref_freq * fractional_bandwidth

        chans = np.intp(np.floor(bandwidth / max_𝞓𝝼))
        chans = min(max(1, chans), chan_width.shape[0])

        # Don't re-add if number of channels match previous ones
        if len(spw_chan_freqs) > 0 and spw_chan_freqs[-1].shape[0] == chans:
            continue

        chans = 1 if chans == 0 else chans
        𝞓𝝼 = bandwidth / chans
        spw_chan_widths.append(𝞓𝝼)

        start_freq = ref_freq - (bandwidth / 2.0)
        end_freq = ref_freq + (bandwidth / 2.0)
        new_chan_freqs = np.linspace(start_freq, end_freq, chans)

        spw_chan_freqs.append(new_chan_freqs)

    return np.asarray(spw_chan_widths), spw_chan_freqs


class Binner(object):
    def __init__(self, row_start, row_end,
                 l, m, n_max,
                 ref_freq,
                 decorrelation,
                 max_uvw_dist):
        # Index of the time bin to which all rows in the bin will contribute
        self.tbin = 0
        # Number of rows in the bin
        self.bin_count = 0
        # Number of flagged rows in the bin
        self.bin_flag_count = 0
        # Starting row of the bin
        self.rs = row_start
        # Ending row of the bin
        self.re = row_end
        # Sinc of baseline speed
        self.bin_sinc_Δψ = 0.0

        # Quantities cached to make Binner.method arguments smaller
        self.ref_freq = ref_freq
        self.l = l  # noqa
        self.m = m
        self.n_max = n_max
        self.decorrelation = decorrelation
        self.max_uvw_dist = max_uvw_dist

        # TODO(sjperkins)
        # Document this
        self.spw = bin_spw_dt(-1)

    def reset(self):
        self.__init__(0, 0,
                      self.l, self.m, self.n_max,
                      self.ref_freq,
                      self.decorrelation, self.max_uvw_dist)

    def start_bin(self, row):
        self.rs = row
        self.re = row
        self.bin_count = 1

    def add_row(self, row, time, interval, uvw):
        """
        Attempts to add ``row`` to the current bin.

        Returns
        -------
        success : bool
            True if the decorrelation tolerance was not exceeded
            and the row was added to the bin.
        """
        rs = self.rs
        re = self.re
        # Evaluate the degree of decorrelation
        # the sample would add to existing bin
        dt = (time[row] + (interval[row] / 2.0) -
              (time[rs] - interval[rs] / 2.0))
        du = uvw[row, 0] - uvw[rs, 0]
        dv = uvw[row, 1] - uvw[rs, 1]

        du_dt = self.l * du / dt
        dv_dt = self.m * dv / dt

        # Derive phase difference in time
        # from Equation (33) in Atemkeng
        # without factor of 2
        𝞓𝞇 = np.pi * (du_dt + dv_dt)
        sinc_𝞓𝞇 = 1.0 if 𝞓𝞇 == 0.0 else np.sin(𝞓𝞇) / 𝞓𝞇

        # We're not decorrelated at this point,
        # Add the row by making it the end of the bin
        # and keep a record of the sinc_𝞓𝞇
        if sinc_𝞓𝞇 > self.decorrelation:
            self.bin_sinc_Δψ = sinc_𝞓𝞇
            self.re = row
            self.bin_count += 1
            return True

        # Adding row to the bin would decorrelate it,
        # so we indicate we did not
        return False

    def finalise_bin(self, uvw,
                     chan_width, chan_freq,
                     spw_chan_width):
        """ Finalise the contents of this bin """
        if self.bin_count == 0:
            return False

        rs = self.rs
        re = self.re

        # Handle special case of bin containing a single sample.
        # Change in baseline speed 𝞓𝞇 == 0
        if self.bin_count == 1:
            if rs != re:
                raise ValueError("single row in bin, but "
                                 "start row != end row")

            du = uvw[rs, 0]
            dv = uvw[rs, 1]
            dw = uvw[rs, 2]
            bin_sinc_𝞓𝞇 = 1.0
        else:
            # duvw between start and end row
            du = uvw[rs, 0] - uvw[re, 0]
            dv = uvw[rs, 1] - uvw[re, 1]
            dw = uvw[rs, 2] - uvw[re, 2]
            bin_sinc_𝞓𝞇 = self.bin_sinc_Δψ

        max_abs_dist = np.sqrt(np.abs(du)*np.abs(self.l) +
                               np.abs(dv)*np.abs(self.m) +
                               np.abs(dw)*np.abs(self.n_max))

        # Derive fractional bandwidth 𝞓𝝼/𝝼
        # from Equation (44) in Atemkeng
        if max_abs_dist == 0.0:
            raise ValueError("max_abs_dist == 0.0")

        # Given
        #   (1) acceptable decorrelation
        #   (2) change in baseline speed
        # derive the frequency phase difference
        # from Equation (35) in Atemkeng
        sinc_𝞓𝞍 = self.decorrelation / bin_sinc_𝞓𝞇

        𝞓𝞍 = inv_sinc(sinc_𝞓𝞍)
        fractional_bandwidth = 𝞓𝞍 / max_abs_dist

        # Derive max_𝞓𝝼, the maximum change in bandwidth
        # before decorrelation occurs in frequency
        #
        # Fractional Bandwidth is defined by
        # https://en.wikipedia.org/wiki/Bandwidth_(signal_processing)
        # for Wideband Antennas as:
        #   (1) 𝞓𝝼/𝝼 = fb = (fh - fl) / (fh + fl)
        # where fh and fl are the high and low frequencies
        # of the band.
        # We set fh = ref_freq + 𝞓𝝼/2, fl = ref_freq - 𝞓𝝼/2
        # Then, simplifying (1), 𝞓𝝼 = 2 * ref_freq * fb
        max_𝞓𝝼 = 2 * self.ref_freq * fractional_bandwidth

        s = np.searchsorted(spw_chan_width, max_𝞓𝝼, side='right') - 1
        assert spw_chan_width[s] <= max_𝞓𝝼

        start_chan = 0
        chan_bin = 0
        bin_𝞓𝝼 = chan_width.dtype.type(0)

        for c in range(1, chan_freq.shape[0]):
            bin_𝞓𝝼 = chan_width[c] - chan_width[start_chan]

            if bin_𝞓𝝼 > spw_chan_width[s]:
                start_chan = c
                chan_bin += 1

        if bin_𝞓𝝼 > 0:
            start_chan = c
            chan_bin += 1

        self.tbin += 1
        self.spw = s
        self.bin_count = 0
        self.bin_flag_count = 0

        return True


@generated_jit(nopython=True, nogil=True, cache=True)
def atemkeng_mapper(time, interval, ant1, ant2, uvw,
                    ref_freq, chan_freq, chan_width,
                    max_uvw_dist,
                    lm_max=1.0, decorrelation=0.98):

    Omitted = numba.types.misc.Omitted

    decorr_type = (numba.typeof(decorrelation.value)
                   if isinstance(decorrelation, Omitted)
                   else decorrelation)

    lm_type = (numba.typeof(lm_max.value)
               if isinstance(lm_max, Omitted)
               else lm_max)

    spec = [
        ('tbin', numba.uintp),
        ('bin_count', numba.uintp),
        ('bin_flag_count', numba.uintp),
        ('rs', numba.uintp),
        ('re', numba.uintp),
        ('bin_sinc_Δψ', uvw.dtype),
        ('l', lm_type),
        ('m', lm_type),
        ('n_max', lm_type),
        ('ref_freq', ref_freq),
        ('decorrelation', decorr_type),
        ('max_uvw_dist', max_uvw_dist),
        ('spw', bin_spw_dt)]

    JitBinner = jitclass(spec)(Binner)

    def _impl(time, interval, ant1, ant2, uvw,
              ref_freq, chan_freq, chan_width,
              max_uvw_dist,
              lm_max=1, decorrelation=0.98):
        # 𝞓 𝝿 𝞇 𝞍 𝝼

        if decorrelation < 0.0 or decorrelation > 1.0:
            raise ValueError("0.0 <= decorrelation <= 1.0 must hold")

        l = m = np.sqrt(0.5 * lm_max)  # noqa
        n_term = 1.0 - l**2 - m**2
        n_max = np.sqrt(n_term) - 1.0 if n_term >= 0.0 else -1.0

        ubl, _, bl_inv, _ = unique_baselines(ant1, ant2)
        utime, _, time_inv, _ = unique_time(time)

        nrow = time.shape[0]
        ntime = utime.shape[0]
        nbl = ubl.shape[0]

        nspws = 16
        res = partition_frequency(nspws, chan_freq, chan_width, ref_freq,
                                  max_uvw_dist, decorrelation)

        spw_chan_width, spw_chan_freqs = res

        # for scw, schans in list(zip(spw_chan_width, [scf.shape[0] for scf in spw_chan_freqs])):
        #     print(schans, scw)

        sentinel = np.finfo(time.dtype).max

        binner = JitBinner(0, 0, l, m, n_max, ref_freq,
                           decorrelation, max_uvw_dist)

        # Create the row lookup
        row_lookup = np.full((nbl, ntime), -1, dtype=np.int32)
        bin_lookup = np.full((nbl, ntime), -1, dtype=np.int32)
        # CLAIM(sjperkins)
        # We'll never have more than 2**16 SPW's!
        bin_spw = np.full((nbl, ntime), -1, dtype=bin_spw_dt)
        out_rows = 0

        for r in range(nrow):
            t = time_inv[r]
            bl = bl_inv[r]

            if row_lookup[bl, t] != -1:
                raise ValueError("Duplicate (TIME, ANTENNA1, ANTENNA2)")

            row_lookup[bl, t] = r

        for bl in range(nbl):
            # Reset the binner for this baseline
            binner.reset()

            for t in range(ntime):
                # Lookup row, continue if non-existent
                r = row_lookup[bl, t]

                if r == -1:
                    continue

                # We're starting a new bin,
                if binner.bin_count == 0:
                    binner.start_bin(r)
                # Try add the row to the bin
                # If this fails, finalise the current bin and start a new one
                elif not binner.add_row(r, time, interval, uvw):
                    if binner.finalise_bin(uvw, chan_width,
                                           chan_freq, spw_chan_width):

                        binner.start_bin(r)

                # Record the bin and spw associated with this row
                bin_lookup[t, bl] = binner.tbin
                bin_spw[t, bl] = binner.spw

            # Finalise any remaining data in the bin
            binner.finalise_bin(uvw, chan_width, chan_freq,
                                spw_chan_width)

            bin_lookup[t, bl] = binner.tbin
            bin_spw[t, bl] = binner.spw
            out_rows += binner.tbin

    return _impl
