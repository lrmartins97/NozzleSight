# nozzle_detector.py - detetor da ponta do bico por simetria radial (kTAMV)
#
# A ponta vista de baixo e' um conjunto de circulos concentricos (furo, parede,
# borda). O detetor usa essa geometria em vez de procurar "a zona mais nitida"
# (que, com pontas de inox, e' muitas vezes um reflexo):
#   1. VOTACAO: cada pixel de borda vota, ao longo do seu gradiente, nos pontos
#      a distancia rmin..rmax. Circulos concentricos acumulam votos no centro;
#      reflexos e linhas retas nao.
#   2. EVIDENCIA DE ANEL: para cada candidato mede-se, em cada raio, a MEDIANA
#      (a volta do circulo) do gradiente radial. Um anel completo tem mediana
#      alta; um reflexo que so ocupa uma parte do circulo nao.
#   3. CENTRO POR GRADIENTES: cada pixel de borda da coroa do anel define uma
#      reta (direcao do gradiente); o centro e' o ponto mais proximo de todas
#      (minimos quadrados com pesos robustos). Sub-pixel, independente da
#      polaridade (furo claro ou escuro) e de o anel estar completo.
#   4. AFINACAO PELA LINHA ESCURA: se a ponta tiver uma linha escura fina e
#      nitida (a aresta interior da agulha), segue-a em coordenadas polares com
#      precisao sub-pixel. E' o metodo mais estavel quando existe.
# Devolve None quando a evidencia nao chega: preferimos "nao vi" a um centro errado.
import cv2
import numpy as np


class NozzleDetector:
    def __init__(self, rmin=10, rmax=70, min_evidence=12.0, min_coverage=0.7,
                 valley_min_depth=1.0, line_min_used=0.5, line_max_rms=0.4, edge_min_slope=2.0):
        self.rmin, self.rmax = rmin, rmax
        self.min_evidence = min_evidence
        self.min_coverage = min_coverage
        self.valley_min_depth = valley_min_depth
        self.line_min_used = line_min_used
        self.line_max_rms = line_max_rms
        self.edge_min_slope = edge_min_slope
        # Memoria das linhas vistas nas imagens anteriores (mesma ferramenta, mesma
        # sequencia de imagens): cada linha e' (polaridade, raio medio, peso).
        # A escolha passa a favorecer a linha que tem aparecido de forma
        # consistente, e deixa de saltar entre aneis de imagem para imagem.
        # Criar um NozzleDetector novo (ou chamar reset()) para cada ferramenta.
        self.features = []
        self.locked = None      # (polaridade, raio) da linha escolhida para esta sequencia
        # Raio da linha fixada na ferramenta anterior (as duas pontas sao do mesmo
        # tipo): a aresta do furo, no plano da ponta, tem o mesmo raio nas duas.
        # Serve para escolher essa aresta e nao contornos da pasta dentro do furo.
        self.ref_radius = None
        self.frames_seen = 0    # imagens com linhas candidatas desde o ultimo reset

    # Escolha da linha: observa LOCK_FRAMES imagens e escolhe, entre as linhas
    # ESCURAS (se houver alguma), a que apareceu em mais imagens; em empate, a de
    # melhor qualidade acumulada. Testado com sequencias reais das duas pontas,
    # comecando em pontos diferentes: e' a regra que escolhe sempre a mesma linha.
    LOCK_FRAMES = 8

    def reset(self, ref_radius=None):
        self.features = []
        self.locked = None
        self.frames_seen = 0
        self.ref_radius = ref_radius

    def _feature_index(self, pol, radius):
        for i, (p, r, w, n) in enumerate(self.features):
            if p == pol and abs(r - radius) <= 1.5:
                return i
        return None

    # ---------------------------------------------------------------- base
    @staticmethod
    def _gradients(gray, sigma=1.5):
        g = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), sigma)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        return gx, gy, np.hypot(gx, gy)

    def _vote(self, gx, gy, mag, keep=0.06):
        H, W = mag.shape
        thr = np.quantile(mag, 1 - keep)
        ys, xs = np.nonzero(mag > thr)
        m = mag[ys, xs]
        ux, uy = gx[ys, xs] / m, gy[ys, xs] / m
        w = np.sqrt(m)
        acc = np.zeros(H * W, np.float32)
        for r in np.arange(self.rmin, self.rmax, 2.0):     # de 2 em 2 px: mesma fiabilidade, 2x mais rapido
            for s in (1.0, -1.0):
                px = np.rint(xs + s * r * ux).astype(np.int64)
                py = np.rint(ys + s * r * uy).astype(np.int64)
                ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
                acc += np.bincount(py[ok] * W + px[ok], weights=w[ok], minlength=H * W).astype(np.float32)
        return cv2.GaussianBlur(acc.reshape(H, W), (0, 0), 2.0)

    def _ring_evidence(self, gx, gy, mag, c, nang=180):
        cx, cy = c
        rs = np.arange(self.rmin, self.rmax + 1, 1.0)
        th = np.linspace(0, 2 * np.pi, nang, endpoint=False)
        ct, st = np.cos(th), np.sin(th)
        H, W = mag.shape
        X = np.clip(np.rint(cx + rs[:, None] * ct).astype(int), 0, W - 1)
        Y = np.clip(np.rint(cy + rs[:, None] * st).astype(int), 0, H - 1)
        rad = np.abs(gx[Y, X] * ct + gy[Y, X] * st)
        rad = np.maximum(np.maximum(rad, np.roll(rad, 1, axis=0)), np.roll(rad, -1, axis=0))
        med = np.median(rad, axis=1)
        return rs, med

    def _gradient_center(self, gx, gy, mag, c0, rin, rout, c_px=2.5, iters=25):
        H, W = mag.shape
        cx, cy = c0
        wt = px = py = None
        for it in range(iters):
            x0, x1 = int(max(cx - rout - 2, 0)), int(min(cx + rout + 3, W))
            y0, y1 = int(max(cy - rout - 2, 0)), int(min(cy + rout + 3, H))
            yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float64)
            GX, GY, M = gx[y0:y1, x0:x1], gy[y0:y1, x0:x1], mag[y0:y1, x0:x1]
            d = np.hypot(xx - cx, yy - cy)
            sel = (d >= rin) & (d <= rout)
            if sel.sum() < 50:
                return None, None
            sel &= M > np.quantile(M[sel], 0.5)
            px, py, m = xx[sel], yy[sel], M[sel].astype(np.float64)
            nx, ny = -GY[sel] / m, GX[sel] / m          # normal a reta do gradiente
            dist = (cx - px) * nx + (cy - py) * ny
            wt = np.where(np.abs(dist) < c_px, (1 - (dist / c_px) ** 2) ** 2, 0.0) * m * m
            k = nx * px + ny * py
            A11, A12, A22 = np.sum(wt * nx * nx), np.sum(wt * nx * ny), np.sum(wt * ny * ny)
            b1, b2 = np.sum(wt * nx * k), np.sum(wt * ny * k)
            det = A11 * A22 - A12 * A12
            if det <= 1e-9:
                return None, None
            ncx, ncy = (A22 * b1 - A12 * b2) / det, (A11 * b2 - A12 * b1) / det
            moved = np.hypot(ncx - cx, ncy - cy)
            cx, cy = ncx, ncy
            if moved < 0.002:
                break
        use = wt > 0
        ang = np.arctan2(py[use] - cy, px[use] - cx)
        cover = len(np.unique(((ang + np.pi) / (2 * np.pi) * 36).astype(int) % 36)) / 36.0
        return (float(cx), float(cy)), {'coverage': cover, 'edge_px': int(use.sum())}

    # ----------------------------------------------------- linha escura fina
    @staticmethod
    def _polar(g, c, rs, nang=360):
        th = np.linspace(0, 2 * np.pi, nang, endpoint=False)
        X = (c[0] + rs[:, None] * np.cos(th)).astype(np.float32)
        Y = (c[1] + rs[:, None] * np.sin(th)).astype(np.float32)
        return th, cv2.remap(g, X, Y, cv2.INTER_CUBIC)

    def _extrema(self, g, c, r):
        """Contornos circulares finos a volta de c, no perfil radial medio:
          pol -1: linha escura (vale)      pol +1: linha clara (crista)
          pol -2: borda claro->escuro      pol +2: borda escuro->claro (para fora)
        Devolve [(raio, contraste, pol)]. As bordas cobrem pontas em que o furo
        e' um degrau (furo escuro, aresta clara) sem linha fina a seguir."""
        rs = np.arange(max(r - 12, 2), r + 12, 0.25)
        _, P = self._polar(g, c, rs)
        base = cv2.GaussianBlur(P.mean(axis=1).reshape(-1, 1), (1, 5), 0).ravel()
        out = []
        for pol in (-1, 1):
            prof = -base if pol < 0 else base
            for k in range(6, len(prof) - 6):
                if prof[k] >= prof[k - 1] and prof[k] >= prof[k + 1]:
                    depth = prof[k] - max(prof[max(k - 16, 0):k].min(), prof[k + 1:k + 17].min())
                    if depth >= self.valley_min_depth:
                        out.append((float(rs[k]), float(depth), pol))
        d = np.gradient(base) / 0.25                    # derivada em niveis por pixel
        for pol in (-2, 2):
            dd = -d if pol < 0 else d
            for k in range(6, len(dd) - 6):
                if dd[k] >= dd[k - 1] and dd[k] >= dd[k + 1] and dd[k] >= self.edge_min_slope:
                    out.append((float(rs[k]), float(dd[k] * 2.0), pol))   # contraste ~ degrau em 2 px
        return out

    def _line_center(self, g, c, r, polarity=-1, halfwin=3.0, iters=10):
        cx, cy = c
        rr_fit = r
        for it in range(iters):
            rs = np.arange(rr_fit - halfwin, rr_fit + halfwin + 1e-6, 0.25)
            th, P = self._polar(g, (cx, cy), rs)
            if abs(polarity) == 2:
                D = np.gradient(P, axis=0)
                prof = -D if polarity < 0 else D
            else:
                prof = -P if polarity < 0 else P
            k = np.clip(np.argmax(prof, axis=0), 1, len(rs) - 2)
            j = np.arange(len(th))
            y0, y1, y2 = prof[k - 1, j], prof[k, j], prof[k + 1, j]
            den = y0 - 2 * y1 + y2
            off = np.where(np.abs(den) > 1e-6, 0.5 * (y0 - y2) / np.where(den == 0, 1, den), 0.0)
            rr = rs[k] + np.clip(off, -1, 1) * 0.25
            depth = y1 - 0.5 * (prof[0, j] + prof[-1, j])
            edge = (k > 1) & (k < len(rs) - 2)
            good = edge & (depth > np.quantile(depth, 0.25))
            if good.sum() < 120:
                return None, None
            # r(theta) = r0 + a cos + b sin + c cos2 + d sin2
            # (a, b) = erro do centro; (c, d) absorvem a elipse (a ponta vista
            # ligeiramente de lado, ou pixeis nao quadrados, nao e' um circulo
            # perfeito e sem estes termos o centro em X ficava mal definido)
            A = np.c_[np.ones(len(th)), np.cos(th), np.sin(th), np.cos(2 * th), np.sin(2 * th)]
            base_w = np.where(good, np.clip(depth, 0, None), 0.0)
            w = base_w.copy()
            for _ in range(6):
                sw = np.sqrt(w)
                sol, *_ = np.linalg.lstsq(A * sw[:, None], rr * sw, rcond=None)
                res = rr - A @ sol
                s = 1.4826 * np.median(np.abs(res[good])) + 0.05
                cc = 3 * s
                w = np.where(good & (np.abs(res) < cc), (1 - (res / cc) ** 2) ** 2, 0.0) * base_w
            rr_fit, a, b = sol[0], sol[1], sol[2]
            cx, cy = cx + a, cy + b
            if abs(a) < 0.002 and abs(b) < 0.002:
                break
        rms = float(np.sqrt(np.sum(w * res ** 2) / max(np.sum(w), 1e-9)))
        ell = float(np.hypot(sol[3], sol[4]))
        return (float(cx), float(cy)), {'used': float((w > 0).mean()), 'rms': rms, 'radius': float(rr_fit),
                                        'ellipticity': ell}

    # ------------------------------------------------------------- principal
    def detect(self, gray, expected_radius=None):
        """expected_radius (px, opcional): raio da linha do furo ja conhecido (por
        exemplo de imagens anteriores da mesma ferramenta). Com ele, escolhe-se a
        linha mais proxima desse raio em vez da mais interior."""
        g = gray.astype(np.float32)
        gx, gy, mag = self._gradients(g)
        a = self._vote(gx, gy, mag)
        best = None
        for _ in range(6):
            _, mx, _, p = cv2.minMaxLoc(a)
            if mx <= 0:
                break
            cv2.circle(a, p, 12, 0, -1)
            rs, med = self._ring_evidence(gx, gy, mag, p)
            k = int(np.argmax(med))
            if best is None or med[k] > best[0]:
                best = (float(med[k]), p, float(rs[k]))
        if best is None or best[0] < self.min_evidence:
            return None
        _, p, r = best
        c, info = self._gradient_center(gx, gy, mag, p, max(self.rmin * 0.5, r - 10), r + 12)
        if c is None or info['coverage'] < self.min_coverage:
            return None
        rs, med = self._ring_evidence(gx, gy, mag, c)
        k = int(np.argmax(med))
        r, evidence = float(rs[k]), float(med[k])
        result = {'center': c, 'radius': r, 'evidence': evidence, 'method': 'gradient',
                  'coverage': info['coverage']}
        # Afinacao por uma linha fina do anel (escura ou clara). Ajusta todas as
        # candidatas e fica com a mais bem definida: menor erro de ajuste face ao
        # seu contraste (rms / sqrt(contraste)). Assim funciona com o furo cheio de
        # pasta (linha escura forte) e vazio (onde a linha util pode ser outra).
        gs = cv2.GaussianBlur(g, (0, 0), 1.0)
        cands = []
        for rv, depth, pol in self._extrema(gs, c, r):
            if expected_radius is not None and abs(rv - expected_radius) > 2.5:
                continue
            c2, i2 = self._line_center(gs, c, rv, polarity=pol)
            max_rms = self.line_max_rms * (1.5 if abs(pol) == 2 else 1.0)
            if c2 is None or i2['used'] < self.line_min_used or i2['rms'] > max_rms:
                continue
            score = i2['rms'] / np.sqrt(min(depth, 12.0))
            cands.append((score, c2, i2, depth, pol))
        if cands:
            # peso acumulado de cada candidata (linhas ja vistas antes pesam mais)
            def history(cand):
                i = self._feature_index(cand[4], cand[2]['radius'])
                return self.features[i][2] if i is not None else 0.0
            best = max(cands, key=lambda cd: (history(cd), -cd[0]))
            # atualiza a memoria com todas as candidatas desta imagem
            counted = set()     # cada linha conta uma vez por imagem
            for score, c2, i2, depth, pol in cands:
                w = 1.0 / max(score, 1e-3)
                i = self._feature_index(pol, i2['radius'])
                if i is None:
                    self.features.append([pol, i2['radius'], w, 1])
                    counted.add(len(self.features) - 1)
                else:
                    p0, r0, w0, n0 = self.features[i]
                    self.features[i] = [p0, (r0 * w0 + i2['radius'] * w) / (w0 + w), w0 + w,
                                        n0 + (0 if i in counted else 1)]
                    counted.add(i)
            # Fixacao da linha, sempre pela mesma regra (ver LOCK_FRAMES). Linhas
            # diferentes da mesma ponta podem ter centros diferentes (ate ~100 um),
            # por isso depois de fixada nunca se troca. Numa imagem sem a linha
            # fixada, o resultado e' "sem linha" e o kTAMV tira outra imagem.
            self.frames_seen += 1
            if self.locked is None and self.frames_seen >= self.LOCK_FRAMES:
                pool = []
                if self.ref_radius is not None:
                    pool = [f for f in self.features if abs(f[1] - self.ref_radius) <= 2.5]
                if not pool:
                    pool = [f for f in self.features if f[0] == -1] or self.features
                f = max(pool, key=lambda f: (f[3], f[2]))
                self.locked = (f[0], f[1])
            if self.locked is not None:
                lp, lr = self.locked
                mine = [cd for cd in cands if cd[4] == lp and abs(cd[2]['radius'] - lr) <= 1.5]
                best = min(mine, key=lambda cd: cd[0]) if mine else None
            else:
                best = None     # ainda a conhecer a ponta: sem medicao fina ate fixar a linha
        if cands and best is not None:
            _, c2, i2, depth, pol = best
            result.update(center=c2, radius=i2['radius'], method='line', line_rms=i2['rms'],
                          line_used=i2['used'], valley_depth=depth, polarity=pol,
                          ellipticity=i2['ellipticity'])
        return result
