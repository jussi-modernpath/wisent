"""Layer 3: disturbance imaging and localization from link motion energies.

Two estimators, both precomputed so live use is a matrix multiply:

- image(): classic Tikhonov-regularized RTI inversion (Wilson & Patwari 2010) with
  elliptical link weights — good for *display* heatmaps.
- locate(): log-domain matched-field scoring against a bistatic proximity kernel
  s_l(p) = 1/(r_tx * r_rx). Pearson correlation in log space is invariant to the
  unknown energy-decay exponent (E ~ s^gamma matches for any gamma > 0), which makes
  it robust to exactly the model mismatch that breaks naive RTI argmax with few
  nodes. Chosen empirically in M0 validation: with 5 nodes / 10 links the RTI argmax
  collapses onto node positions, matched-field does not. Revisit against real data (M2).
"""

import numpy as np


def _diff_op(nx: int, ny: int, axis: int) -> np.ndarray:
    idx = np.arange(nx * ny).reshape(ny, nx)
    if axis == 0:  # x-neighbours
        a, b = idx[:, :-1].ravel(), idx[:, 1:].ravel()
    else:          # y-neighbours
        a, b = idx[:-1, :].ravel(), idx[1:, :].ravel()
    D = np.zeros((len(a), nx * ny))
    D[np.arange(len(a)), a] = -1.0
    D[np.arange(len(a)), b] = 1.0
    return D


class VRTI:
    def __init__(self, links, xlim, ylim, nx=24, ny=24,
                 ellipse_excess=0.4, alpha=10.0):
        """links: list of (tx_xy, rx_xy) pairs. xlim/ylim: room extent in meters.

        ellipse_excess: bistatic excess-path threshold (m) defining each link's
        sensitive ellipse. alpha: Tikhonov smoothness weight — tune on real data (M2).
        """
        self.links = [(np.asarray(a, float), np.asarray(b, float)) for a, b in links]
        xs = np.linspace(xlim[0], xlim[1], nx)
        ys = np.linspace(ylim[0], ylim[1], ny)
        self.X, self.Y = np.meshgrid(xs, ys)
        self.shape = self.X.shape
        vox = np.stack([self.X.ravel(), self.Y.ravel()], axis=1)

        W = np.zeros((len(self.links), vox.shape[0]))
        for i, (tx, rx) in enumerate(self.links):
            d = np.linalg.norm(tx - rx)
            excess = (np.linalg.norm(vox - tx, axis=1)
                      + np.linalg.norm(vox - rx, axis=1)) - d
            W[i, excess < ellipse_excess] = 1.0 / np.sqrt(max(d, 0.1))

        Dx = _diff_op(nx, ny, 0)
        Dy = _diff_op(nx, ny, 1)
        # errstate: macOS Accelerate BLAS raises spurious divide-by-zero/overflow/invalid
        # on *any* matmul of this size (an all-zeros @ all-zeros product warns too), so
        # the flags say nothing about our data. Assert finiteness instead — that check is
        # real and will fire if link geometry or alpha ever produce a degenerate system.
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            A = W.T @ W + alpha * (Dx.T @ Dx + Dy.T @ Dy)
            self.P = np.linalg.solve(A, W.T)  # (V, L), precomputed projection
        if not np.isfinite(A).all() or not np.isfinite(self.P).all():
            raise ValueError("VRTI inversion is not finite — check node geometry and alpha")
        self.W = W

        # Matched-field dictionary: z-scored log sensitivity kernel per voxel.
        # s_l(p) = |g_l(p)| / (r_tx * r_rx): bistatic amplitude times the gradient
        # magnitude |unit(p-tx)+unit(p-rx)|, which vanishes on the LOS segment —
        # motion there barely changes the path length, so links are *blind on their
        # own LOS* (validated in M0: dropping |g| mislocalizes by meters).
        S = self._log_kernel(vox)
        self.S_z = (S - S.mean(axis=0)) / (S.std(axis=0) + 1e-12)

    def _log_kernel(self, pts):
        """(L, n) log sensitivity at arbitrary points — shared by the voxel grid
        and by fit_gains(), so calibration and localization use one model."""
        pts = np.asarray(pts, float)
        K = np.zeros((len(self.links), len(pts)))
        for i, (tx, rx) in enumerate(self.links):
            d1 = pts - tx
            d2 = pts - rx
            r1 = np.linalg.norm(d1, axis=1)
            r2 = np.linalg.norm(d2, axis=1)
            g = d1 / np.maximum(r1, 1e-9)[:, None] + d2 / np.maximum(r2, 1e-9)[:, None]
            gmag = np.maximum(np.linalg.norm(g, axis=1), 0.05)
            K[i] = np.log(gmag / np.maximum(r1 * r2, 0.36))
        return K

    def fit_gains(self, positions, responses, gammas=None):
        """Per-link gain calibration from labelled dwells -> (log_gains, gamma).

        Model: log R_(l,s) = log g_l + gamma * K_l(p_s) + noise, fitted by grid search
        over gamma with per-link closed-form gains. Motivation is measured, not
        theoretical: links respond to the same person with gains differing up to ~10x
        (deep-fade amplification), and on station test #2 the uncalibrated estimator
        degenerated into a nearest-node classifier (2/5 quadrants). With gains fitted
        leave-one-out on the other 4 dwells, the held-out station scored 4/5 quadrants,
        median 3.05 -> 2.24 m. Fitted gamma on real data is ~0.2-0.5, i.e. real
        responses vary far more weakly with geometry than the kernel's implicit
        gamma=1 — most of what the kernel treats as position information is actually
        per-link gain, which is exactly why calibration matters.

        positions: (n, 2) known dwell coordinates. responses: (n, L) linear
        floor-normalized energies. Returns mean-centred log-gains (L,) and gamma;
        pass the gains to locate(gains=...).
        """
        positions = np.asarray(positions, float)
        R = np.log(np.maximum(np.asarray(responses, float), 1e-9))     # (n, L)
        K = self._log_kernel(positions).T                              # (n, L)
        if gammas is None:
            gammas = np.arange(0.2, 3.01, 0.1)
        best = None
        for gamma in gammas:
            g = (R - gamma * K).mean(axis=0)
            sse = float(((R - gamma * K - g) ** 2).sum())
            if best is None or sse < best[0]:
                best = (sse, gamma, g)
        _, gamma, g = best
        return g - g.mean(), float(gamma)

    def image(self, y) -> np.ndarray:
        """y: per-link motion energies (L,). RTI heatmap (ny, nx) — display only."""
        y = np.asarray(y, float)
        y = y / (y.max() + 1e-12)
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):  # see __init__
            return (self.P @ y).reshape(self.shape)

    def locate(self, y, baseline=None, min_excess=1.5, min_margin=0.15, gains=None):
        """Log-domain matched-field position estimate -> (xy_estimate, score_map).

        Exponent-invariant: any monotone power-law between motion energy and the
        proximity kernel gives the same argmax.

        **Returns (None, score_map) when the evidence cannot support a position.** The
        unguarded version always returned a coordinate — on a genuinely empty room it
        produced an arbitrary corner, which is indistinguishable from a real detection to
        anything downstream (observed on hardware 2026-07-29). Two gates:

        - `baseline`: per-link empty-room floors. Link floors were measured to differ
          **6x** across a real deployment (a through-clutter link vs a clean one), so raw
          energies are not comparable between links and MUST be divided by their own
          floor first. Without a baseline the ranking is dominated by which link is
          noisiest, not by where the disturbance is.
        - `min_excess`: at least one link must exceed `min_excess` x its floor, else the
          scene is judged unobservable and None is returned.
        - `min_margin`: the peak score must beat the field's spread by this margin,
          otherwise the map is flat and the argmax is arbitrary.
        """
        y = np.asarray(y, float)
        if baseline is not None:
            y = y / np.maximum(np.asarray(baseline, float), 1e-12)
            if np.max(y) < min_excess:
                return None, np.zeros(self.shape)      # nothing is moving
        if gains is not None:
            # Divide fitted per-link gains out of the FULL ratio. An excess-only
            # variant (y = 1 + (y-1)*exp(-g)) is theoretically cleaner — the floor is
            # receiver noise and carries no gain — but BOTH variants were tested
            # leave-one-out on station2 (5 labelled dwells) and full-ratio won
            # decisively: median 0.75 m / 5-5 quadrants vs 2.96 m / 4-5. Empirics
            # over elegance, flagged OPEN: re-decide on the denser dictionary run,
            # where n>5 can actually settle it. Applied after the min_excess gate,
            # which is floor-relative and stays uncalibrated.
            y = y * np.exp(-np.asarray(gains, float))
        logy = np.log(np.maximum(y, 1e-12))
        sd = logy.std()
        if sd < 1e-6:
            return None, np.zeros(self.shape)          # flat evidence carries no position
        yz = (logy - logy.mean()) / (sd + 1e-12)
        if baseline is not None:
            # Abstention weights: a link at its own empty-room floor has no evidence and
            # must not vote z-scored noise with the same weight as a link at 14x floor.
            # Weight by excess-over-floor; links with none abstain. (This does NOT fix
            # the measured prior-dominated failure at 7 links — see roadmap M2 — but it
            # stops no-evidence links from diluting the links that do see something.)
            wts = np.maximum(y - 1.0, 0.0)
            if wts.sum() <= 0:
                return None, np.zeros(self.shape)
            wts = wts / wts.sum()
            score = (wts[:, None] * yz[:, None] * self.S_z).sum(axis=0)
        else:
            score = (yz[:, None] * self.S_z).mean(axis=0)
        smax, smean, sstd = score.max(), score.mean(), score.std()
        if sstd < 1e-9 or (smax - smean) / (sstd + 1e-12) < min_margin:
            return None, score.reshape(self.shape)     # no peak worth naming
        j = int(np.argmax(score))
        return (np.array([self.X.ravel()[j], self.Y.ravel()[j]]),
                score.reshape(self.shape))
