# -*- coding: utf-8 -*-

from collections import namedtuple

import numpy as np

from africanus.averaging.bda_mapping import (atemkeng_mapper,
                                             RowMapOutput)
from africanus.averaging.shared import (chan_corrs,
                                        merge_flags,
                                        vis_output_arrays)
from africanus.util.numba import (generated_jit,
                                  intrinsic,
                                  is_numba_type_none)


_row_output_fields = ["antenna1", "antenna2", "time_centroid", "exposure",
                      "uvw", "weight", "sigma"]
RowAverageOutput = namedtuple("RowAverageOutput", _row_output_fields)


@generated_jit(nopython=True, nogil=True, cache=True)
def row_average(meta, ant1, ant2, flag_row=None,
                time_centroid=None, exposure=None, uvw=None,
                weight=None, sigma=None):

    have_flag_row = not is_numba_type_none(flag_row)
    have_time_centroid = not is_numba_type_none(time_centroid)
    have_exposure = not is_numba_type_none(exposure)
    have_uvw = not is_numba_type_none(uvw)
    have_weight = not is_numba_type_none(weight)
    have_sigma = not is_numba_type_none(sigma)

    def impl(meta, ant1, ant2, flag_row=None,
             time_centroid=None, exposure=None, uvw=None,
             weight=None, sigma=None):

        out_rows = meta.time.shape[0]

        counts = np.zeros(out_rows, dtype=np.uint32)

        # These outputs are always present
        ant1_avg = np.empty(out_rows, ant1.dtype)
        ant2_avg = np.empty(out_rows, ant2.dtype)

        # Possibly present outputs for possibly present inputs
        uvw_avg = (
            None if not have_uvw else
            np.zeros((out_rows,) + uvw.shape[1:],
                     dtype=uvw.dtype))

        time_centroid_avg = (
            None if not have_time_centroid else
            np.zeros((out_rows,) + time_centroid.shape[1:],
                     dtype=time_centroid.dtype))

        exposure_avg = (
            None if not have_exposure else
            np.zeros((out_rows,) + exposure.shape[1:],
                     dtype=exposure.dtype))

        weight_avg = (
            None if not have_weight else
            np.zeros((out_rows,) + weight.shape[1:],
                     dtype=weight.dtype))

        sigma_avg = (
            None if not have_sigma else
            np.zeros((out_rows,) + sigma.shape[1:],
                     dtype=sigma.dtype))

        sigma_weight_sum = (
            None if not have_sigma else
            np.zeros((out_rows,) + sigma.shape[1:],
                     dtype=sigma.dtype))

        # Average each array, if present
        # The output is a flattened row-channel array
        # where the values for each row are repeated along the channel
        # Individual runs in this output are described by
        # meta.offset and meta.num_chan
        # Thus, we only compute the sum in the first position
        for ri in range(meta.map.shape[0]):
            ro = meta.map[ri, 0]

            # Here we can simply assign because input_row baselines
            # should always match output row baselines
            ant1_avg[ro] = ant1[ri]
            ant2_avg[ro] = ant2[ri]

            # Input and output flags must match in order for the
            # current row to contribute to these columns
            if have_flag_row and flag_row[ri] != meta.flag_row[ro]:
                continue

            counts[ro] += 1

            if have_uvw:
                uvw_avg[ro, 0] += uvw[ri, 0]
                uvw_avg[ro, 1] += uvw[ri, 1]
                uvw_avg[ro, 2] += uvw[ri, 2]

            if have_time_centroid:
                time_centroid_avg[ro] += time_centroid[ri]

            if have_exposure:
                exposure_avg[ro] += exposure[ri]

            if have_weight:
                for co in range(weight.shape[1]):
                    weight_avg[ro, co] += weight[ri, co]

            if have_sigma:
                for co in range(sigma.shape[1]):
                    # Use weights if present else natural weights
                    wt = weight[ri, co] if have_weight else 1.0

                    # Assign
                    sigma_avg[ro, co] += sigma[ri, co]**2 * wt**2
                    sigma_weight_sum[ro, co] += wt

        # Compute the average in the output row position
        # then copy to the other positions for each channel
        for ri in range(meta.map.shape[0]):
            # Normalise the first output position for the input row
            count = counts[meta.map[ri, 0]]
            bro = ro = meta.map[ri, 0]

            if count > 0:
                # Normalise uvw
                if have_uvw:
                    uvw_avg[ro, 0] /= count
                    uvw_avg[ro, 1] /= count
                    uvw_avg[ro, 2] /= count

                # Normalise time centroid
                if have_time_centroid:
                    time_centroid_avg[ro] /= count

                # Normalise sigma
                if have_sigma:
                    for co in range(sigma.shape[1]):
                        ssva = sigma_avg[ro, co]
                        wt = sigma_weight_sum[ro, co]

                        if wt != 0.0:
                            ssva /= (wt**2)

                        sigma_avg[ro, co] = np.sqrt(ssva)

            # Copy first value into all channel positions
            for fi in range(1, meta.map.shape[1]):
                ro = meta.map[ri, fi]
                ant1_avg[ro] = ant1[bro]
                ant2_avg[ro] = ant2[bro]

                if have_uvw:
                    uvw_avg[ro, 0] = uvw_avg[bro, 0]
                    uvw_avg[ro, 1] = uvw_avg[bro, 1]
                    uvw_avg[ro, 2] = uvw_avg[bro, 2]

                if have_time_centroid:
                    time_centroid_avg[ro] = time_centroid_avg[bro]

                if have_exposure:
                    exposure_avg[ro] = exposure_avg[bro]

                if have_weight:
                    for co in range(weight.shape[1]):
                        weight_avg[ro, co] = weight_avg[bro, co]

                if have_sigma:
                    for co in range(sigma.shape[1]):
                        sigma_avg[ro, co] = sigma_avg[bro, co]

        return RowAverageOutput(ant1_avg, ant2_avg,
                                time_centroid_avg,
                                exposure_avg, uvw_avg,
                                weight_avg, sigma_avg)

    return impl


_rowchan_output_fields = ["vis", "flag", "weight_spectrum", "sigma_spectrum"]
RowChanAverageOutput = namedtuple("RowChanAverageOutput",
                                  _rowchan_output_fields)


class RowChannelAverageException(Exception):
    pass


@intrinsic
def average_visibilities(typingctx, vis, vis_avg, vis_weight_sum,
                         weight, ri, fi, ro, co):

    import numba.core.types as nbtypes

    have_array = isinstance(vis, nbtypes.Array)
    have_tuple = isinstance(vis, (nbtypes.Tuple, nbtypes.UniTuple))

    def avg_fn(vis, vis_avg, vis_ws, wt, ri, fi, ro, co):
        vis_avg[ro, co] += vis[ri, fi, co] * wt
        vis_ws[ro, co] += wt

    return_type = nbtypes.NoneType("none")

    sig = return_type(vis, vis_avg, vis_weight_sum,
                      weight, ri, fi, ro, co)

    def codegen(context, builder, signature, args):
        vis, vis_type = args[0], signature.args[0]
        vis_avg, vis_avg_type = args[1], signature.args[1]
        vis_weight_sum, vis_weight_sum_type = args[2], signature.args[2]
        weight, weight_type = args[3], signature.args[3]
        ri, ri_type = args[4], signature.args[4]
        fi, fi_type = args[5], signature.args[5]
        ro, ro_type = args[6], signature.args[6]
        co, co_type = args[7], signature.args[7]
        return_type = signature.return_type

        if have_array:
            avg_sig = return_type(vis_type,
                                  vis_avg_type,
                                  vis_weight_sum_type,
                                  weight_type,
                                  ri_type, fi_type,
                                  ro_type, co_type)
            avg_args = [vis, vis_avg, vis_weight_sum,
                        weight, ri, fi, ro, co]

            # Compile function and get handle to output
            context.compile_internal(builder, avg_fn,
                                     avg_sig, avg_args)
        elif have_tuple:
            for i in range(len(vis_type)):
                avg_sig = return_type(vis_type.types[i],
                                      vis_avg_type.types[i],
                                      vis_weight_sum_type.types[i],
                                      weight_type,
                                      ri_type, fi_type,
                                      ro_type, co_type)
                avg_args = [builder.extract_value(vis, i),
                            builder.extract_value(vis_avg, i),
                            builder.extract_value(vis_weight_sum, i),
                            weight, ri, fi, ro, co]

                # Compile function and get handle to output
                context.compile_internal(builder, avg_fn,
                                         avg_sig, avg_args)
        else:
            # noop
            pass

    return sig, codegen


@intrinsic
def normalise_visibilities(typingctx, vis_avg, vis_weight_sum, ro, co):
    import numba.core.types as nbtypes

    have_array = isinstance(vis_avg, nbtypes.Array)
    have_tuple = isinstance(vis_avg, (nbtypes.Tuple, nbtypes.UniTuple))

    def normalise_fn(vis_avg, vis_ws, ro, co):
        weight_sum = vis_ws[ro, co]

        if weight_sum != 0.0:
            vis_avg[ro, co] /= weight_sum

    return_type = nbtypes.NoneType("none")
    sig = return_type(vis_avg, vis_weight_sum, ro, co)

    def codegen(context, builder, signature, args):
        vis_avg, vis_avg_type = args[0], signature.args[0]
        vis_weight_sum, vis_weight_sum_type = args[1], signature.args[1]
        ro, ro_type = args[2], signature.args[2]
        co, co_type = args[3], signature.args[3]
        return_type = signature.return_type

        if have_array:
            # Normalise single array
            norm_sig = return_type(vis_avg_type,
                                   vis_weight_sum_type,
                                   ro_type, co_type)
            norm_args = [vis_avg, vis_weight_sum, ro, co]

            context.compile_internal(builder, normalise_fn,
                                     norm_sig, norm_args)
        elif have_tuple:
            # Normalise each array in the tuple
            for i in range(len(vis_avg_type)):
                norm_sig = return_type(vis_avg_type.types[i],
                                       vis_weight_sum_type.types[i],
                                       ro_type, co_type)
                norm_args = [builder.extract_value(vis_avg, i),
                             builder.extract_value(vis_weight_sum, i),
                             ro, co]

                # Compile function and get handle to output
                context.compile_internal(builder, normalise_fn,
                                         norm_sig, norm_args)
        else:
            # noop
            pass

    return sig, codegen


@generated_jit(nopython=True, nogil=True, cache=True)
def rca(meta, flag_row=None, weight=None,
        visibilities=None,
        flag=None,
        weight_spectrum=None,
        sigma_spectrum=None):

    have_vis = not is_numba_type_none(visibilities)
    have_flag = not is_numba_type_none(flag)
    have_flag_row = not is_numba_type_none(flag_row)
    have_flags = have_flag_row or have_flag

    have_weight = not is_numba_type_none(weight)
    have_weight_spectrum = not is_numba_type_none(weight_spectrum)
    have_sigma_spectrum = not is_numba_type_none(sigma_spectrum)

    def impl(meta, flag_row=None, weight=None,
             visibilities=None,
             flag=None,
             weight_spectrum=None,
             sigma_spectrum=None):

        out_rows = meta.time.shape[0]
        nchan, ncorrs = chan_corrs(visibilities, flag,
                                   weight_spectrum, sigma_spectrum,
                                   None, None,
                                   None, None)

        out_shape = (out_rows, ncorrs)

        if not have_flag:
            flag_avg = None
        else:
            flag_avg = np.zeros(out_shape, np.bool_)

        # If either flag_row or flag is present, we need to ensure that
        # effective averaging takes place.
        if have_flags:
            flags_match = np.zeros(meta.map.shape + (ncorrs,), dtype=np.bool_)
            flag_counts = np.zeros(out_shape, dtype=np.uint32)

        counts = np.zeros(out_shape, dtype=np.uint32)

        # Determine output bin counts both unflagged and flagged
        for ri in range(meta.map.shape[0]):
            for fi in range(meta.map.shape[1]):
                ro = meta.map[ri, fi]
                row_flagged = have_flag_row and flag_row[ri] != 0

                for co in range(ncorrs):
                    flagged = (row_flagged or
                               (have_flag and flag[ri, fi, co] != 0))

                    if have_flags:
                        # This sets up one part of a boolean
                        # expression, completed below in the normalisation
                        flags_match[ri, fi, co] = flagged

                    if have_flags and flagged:
                        flag_counts[ro, co] += 1
                    else:
                        counts[ro, co] += 1

        # ------
        # Flags
        # ------

        # Determine whether input samples should contribute to an output bin
        # and, if flags are parent, whether the output bin is flagged

        # This follows from the definition of an effective average:
        #
        # * bad or flagged values should be excluded
        #   when calculating the average
        #
        # Note that if a bin is completely flagged we still compute an average,
        # to which all relevant input samples contribute.
        for ri in range(meta.map.shape[0]):
            for fi in range(meta.map.shape[1]):
                ro = meta.map[ri, fi]

                for co in range(ncorrs):
                    if counts[ro, co] > 0:
                        # Output bin should only contain unflagged samples
                        out_flag = False

                        if have_flag:
                            # Set output flags
                            flag_avg[ro, co] = False

                    elif have_flags and flag_counts[ro, co] > 0:
                        # Output bin is completely flagged
                        out_flag = True

                        if have_flag:
                            # Set output flags
                            flag_avg[ro, co] = True
                    else:
                        raise RowChannelAverageException("Zero-filled bin")

                    # We should only add a sample to an output bin
                    # if the input flag matches the output flag.
                    # This is because flagged samples don't contribute
                    # to a bin with some unflagged samples while
                    # unflagged samples never contribute to a
                    # completely flagged bin
                    if have_flags:
                        match = flags_match[ri, fi, co] == out_flag
                        flags_match[ri, fi, co] = match

        # -------------
        # Visibilities
        # -------------
        if not have_vis:
            vis_avg = None
        else:
            vis_avg, vis_weight_sum = vis_output_arrays(
                visibilities, out_shape)

            # Aggregate
            for ri in range(meta.map.shape[0]):
                for fi in range(meta.map.shape[1]):
                    ro = meta.map[ri, fi]

                    for co in range(ncorrs):
                        if have_flags and not flags_match[ri, fi, co]:
                            continue

                        wt = (weight_spectrum[ri, fi, co]
                              if have_weight_spectrum else
                              weight[ri, co] if have_weight else 1.0)

                        average_visibilities(visibilities,
                                             vis_avg, vis_weight_sum,
                                             wt, ri, fi, ro, co)

            # Normalise
            for ro in range(out_rows):
                for co in range(ncorrs):
                    normalise_visibilities(vis_avg, vis_weight_sum, ro, co)

        # ----------------
        # Weight Spectrum
        # ----------------
        if not have_weight_spectrum:
            weight_spectrum_avg = None
        else:
            weight_spectrum_avg = np.zeros(out_shape, weight_spectrum.dtype)

            # Aggregate
            for ri in range(meta.map.shape[0]):
                for fi in range(meta.map.shape[1]):
                    ro = meta.map[ri, fi]

                    for co in range(ncorrs):
                        if have_flags and not flags_match[ri, fi, co]:
                            continue

                        weight_spectrum_avg[ro, co] += (
                            weight_spectrum[ri, fi, co])

        # ---------------
        # Sigma Spectrum
        # ---------------
        if not have_sigma_spectrum:
            sigma_spectrum_avg = None
        else:
            sigma_spectrum_avg = np.zeros(out_shape, sigma_spectrum.dtype)
            sigma_spectrum_weight_sum = np.zeros_like(sigma_spectrum_avg)

            # Aggregate
            for ri in range(meta.map.shape[0]):
                for fi in range(meta.map.shape[1]):
                    ro = meta.map[ri, fi]

                    for co in range(ncorrs):
                        if have_flags and not flags_match[ri, fi, co]:
                            continue

                        wt = (weight_spectrum[ri, fi, co]
                              if have_weight_spectrum else
                              weight[ri, co] if have_weight else 1.0)

                        ssv = sigma_spectrum[ri, fi, co]**2 * wt**2
                        sigma_spectrum_avg[ro, co] += ssv
                        sigma_spectrum_weight_sum[ro, co] += wt

            # Normalise
            for ro in range(out_rows):
                for co in range(ncorrs):
                    if sigma_spectrum_weight_sum[ro, co] != 0.0:
                        ssv = sigma_spectrum_avg[ro, co]
                        sswsum = sigma_spectrum_weight_sum[ro, co]
                        sigma_spectrum_avg[ro, co] = np.sqrt(ssv / sswsum**2)

        return RowChanAverageOutput(vis_avg, flag_avg,
                                    weight_spectrum_avg,
                                    sigma_spectrum_avg)

    return impl


@generated_jit(nopython=True, nogil=True, cache=True)
def row_chan_average(meta, flag_row=None, weight=None,
                     vis=None, flag=None,
                     weight_spectrum=None, sigma_spectrum=None):

    dummy_chan_freq = None
    dummy_chan_width = None

    have_flag_row = not is_numba_type_none(flag_row)
    have_weight = not is_numba_type_none(weight)

    have_vis = not is_numba_type_none(vis)
    have_flag = not is_numba_type_none(flag)
    have_weight_spectrum = not is_numba_type_none(weight_spectrum)
    have_sigma_spectrum = not is_numba_type_none(sigma_spectrum)

    def impl(meta, flag_row=None, weight=None,
             vis=None, flag=None,
             weight_spectrum=None, sigma_spectrum=None):

        out_rows = meta.time.shape[0]
        nchan, ncorrs = chan_corrs(vis, flag,
                                   weight_spectrum, sigma_spectrum,
                                   dummy_chan_freq, dummy_chan_width,
                                   dummy_chan_width, dummy_chan_width)

        out_shape = (out_rows, ncorrs)

        if have_vis:
            vis_avg = np.zeros(out_shape, dtype=vis.dtype)
            vis_weight_sum = np.zeros(out_shape, dtype=vis.real.dtype)
            flagged_vis_avg = np.zeros_like(vis_avg)
            flagged_vis_weight_sum = np.zeros_like(vis_weight_sum)
        else:
            vis_avg = None
            vis_weight_sum = None
            flagged_vis_avg = None
            flagged_vis_weight_sum = None

        if have_weight_spectrum:
            weight_spectrum_avg = np.zeros(
                out_shape, dtype=weight_spectrum.dtype)
            flagged_weight_spectrum_avg = np.zeros_like(weight_spectrum_avg)
        else:
            weight_spectrum_avg = None
            flagged_weight_spectrum_avg = None

        if have_sigma_spectrum:
            sigma_spectrum_avg = np.zeros(
                out_shape, dtype=sigma_spectrum.dtype)
            sigma_spectrum_weight_sum = np.zeros_like(sigma_spectrum_avg)
            flagged_sigma_spectrum_avg = np.zeros_like(sigma_spectrum_avg)
            flagged_sigma_spectrum_weight_sum = np.zeros_like(
                sigma_spectrum_avg)
        else:
            sigma_spectrum_avg = None
            sigma_spectrum_weight_sum = None
            flagged_sigma_spectrum_avg = None
            flagged_sigma_spectrum_weight_sum = None

        if have_flag:
            flag_avg = np.zeros(out_shape, dtype=flag.dtype)
        else:
            flag_avg = None

        counts = np.zeros(out_shape, dtype=np.uint32)
        flag_counts = np.zeros(out_shape, dtype=np.uint32)

        # Iterate over input rows, accumulating into output rows
        for ri in range(meta.map.shape[0]):
            for fi in range(meta.map.shape[1]):
                ro = meta.map[ri, fi]

                # TIME_CENTROID/EXPOSURE case applies here,
                # must have flagged input and output OR
                # unflagged input and output
                if have_flag_row and flag_row[ri] != meta.flag_row[ro]:
                    continue

                for co in range(ncorrs):
                    flagged = have_flag and flag[ri, fi, co] != 0

                    if flagged:
                        flag_counts[ro, co] += 1
                    else:
                        counts[ro, co] += 1

                    # Aggregate visibilities
                    if have_vis:
                        # Use full-resolution weight spectrum if given
                        # else weights, else natural weights
                        wt = (weight_spectrum[ri, fi, co]
                              if have_weight_spectrum else
                              weight[ri, co] if have_weight else 1.0)

                        iv = vis[ri, fi, co] * wt

                        if flagged:
                            flagged_vis_avg[ro, co] += iv
                            flagged_vis_weight_sum[ro, co] += wt
                        else:
                            vis_avg[ro, co] += iv
                            vis_weight_sum[ro, co] += wt

                    # Weight Spectrum
                    if have_weight_spectrum:
                        if flagged:
                            flagged_weight_spectrum_avg[ro, co] += (
                                weight_spectrum[ri, fi, co])
                        else:
                            weight_spectrum_avg[ro, co] += (
                                weight_spectrum[ri, fi, co])

                    # Sigma Spectrum
                    if have_sigma_spectrum:
                        # Use full-resolution weight spectrum if given
                        # else weights, else natural weights
                        wt = (weight_spectrum[ri, fi, co]
                              if have_weight_spectrum else
                              weight[ri, co] if have_weight else 1.0)

                        ssin = sigma_spectrum[ri, fi, co]**2 * wt**2

                        if flagged:
                            flagged_sigma_spectrum_avg[ro, co] += ssin
                            flagged_sigma_spectrum_weight_sum[ro, co] += wt
                        else:
                            sigma_spectrum_avg[ro, co] += ssin
                            sigma_spectrum_weight_sum[ro, co] += wt

        for ro in range(out_rows):
            for co in range(ncorrs):
                if counts[ro, co] > 0:
                    if have_vis:
                        vwsum = vis_weight_sum[ro, co]
                        vin = vis_avg[ro, co]

                    if have_sigma_spectrum:
                        sswsum = sigma_spectrum_weight_sum[ro, co]
                        ssin = sigma_spectrum_avg[ro, co]

                    flagged = 0
                elif flag_counts[ro, co] > 0:
                    if have_vis:
                        vwsum = flagged_vis_weight_sum[ro, co]
                        vin = flagged_vis_avg[ro, co]

                    if have_sigma_spectrum:
                        sswsum = flagged_sigma_spectrum_weight_sum[ro, co]
                        ssin = flagged_sigma_spectrum_avg[ro, co]

                    flagged = 1
                else:
                    raise RowChannelAverageException("Zero-filled bin")

                # Normalise visibilities
                if have_vis and vwsum != 0.0:
                    vis_avg[ro, co] = vin / vwsum

                # Normalise Sigma Spectrum
                if have_sigma_spectrum and sswsum != 0.0:
                    # sqrt(sigma**2 * weight**2 / (weight(sum**2)))
                    sigma_spectrum_avg[ro, co] = np.sqrt(ssin / sswsum**2)

                # Set flag
                if have_flag:
                    flag_avg[ro, co] = flagged

                # Copy Weights if flagged
                if have_weight_spectrum and flagged:
                    weight_spectrum_avg[ro, co] = (
                        flagged_weight_spectrum_avg[ro, co])

        return RowChanAverageOutput(vis_avg, flag_avg,
                                    weight_spectrum_avg,
                                    sigma_spectrum_avg)

    return impl


_chan_output_fields = ["chan_freq", "chan_width", "effective_bw", "resolution"]
ChannelAverageOutput = namedtuple("ChannelAverageOutput", _chan_output_fields)


AverageOutput = namedtuple("AverageOutput",
                           list(RowMapOutput._fields) +
                           _row_output_fields +
                           # _chan_output_fields +
                           _rowchan_output_fields)


@generated_jit(nopython=True, nogil=True, cache=True)
def bda(time, interval, antenna1, antenna2,
        time_centroid=None, exposure=None, flag_row=None,
        uvw=None, weight=None, sigma=None,
        chan_freq=None, chan_width=None,
        effective_bw=None, resolution=None,
        vis=None, flag=None,
        weight_spectrum=None, sigma_spectrum=None,
        max_uvw_dist=None, max_fov=3.0,
        decorrelation=0.98,
        time_bin_secs=None,
        min_nchan=1):

    def impl(time, interval, antenna1, antenna2,
             time_centroid=None, exposure=None, flag_row=None,
             uvw=None, weight=None, sigma=None,
             chan_freq=None, chan_width=None,
             effective_bw=None, resolution=None,
             vis=None, flag=None,
             weight_spectrum=None, sigma_spectrum=None,
             max_uvw_dist=None, max_fov=3.0,
             decorrelation=0.98,
             time_bin_secs=None,
             min_nchan=1):

        # Merge flag_row and flag arrays
        flag_row = merge_flags(flag_row, flag)

        meta = atemkeng_mapper(time, interval, antenna1, antenna2, uvw,
                               chan_width, chan_freq,
                               max_uvw_dist,
                               flag_row=flag_row,
                               max_fov=max_fov,
                               decorrelation=decorrelation,
                               time_bin_secs=time_bin_secs,
                               min_nchan=min_nchan)

        row_avg = row_average(meta, antenna1, antenna2, flag_row,  # noqa: F841
                              time_centroid, exposure, uvw,
                              weight=weight, sigma=sigma)

        row_chan_avg = row_chan_average(meta,  # noqa: F841
                                        flag_row=flag_row,
                                        vis=vis, flag=flag,
                                        weight_spectrum=weight_spectrum,
                                        sigma_spectrum=sigma_spectrum)

        # Have to explicitly write it out because numba tuples
        # are highly constrained types
        return AverageOutput(meta.map,
                             meta.offsets,
                             meta.num_chan,
                             meta.decorr_chan_width,
                             meta.time,
                             meta.interval,
                             meta.chan_width,
                             meta.flag_row,
                             row_avg.antenna1,
                             row_avg.antenna2,
                             row_avg.time_centroid,
                             row_avg.exposure,
                             row_avg.uvw,
                             row_avg.weight,
                             row_avg.sigma,
                             # None,  # chan_data.chan_freq,
                             # None,  # chan_data.chan_width,
                             # None,  # chan_data.effective_bw,
                             # None,  # chan_data.resolution,
                             row_chan_avg.vis,
                             row_chan_avg.flag,
                             row_chan_avg.weight_spectrum,
                             row_chan_avg.sigma_spectrum)

    return impl
