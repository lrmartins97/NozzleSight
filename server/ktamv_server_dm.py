import copy, time, cv2, numpy as np
from ktamv_server_io import Ktamv_Server_Io as io


import threading, os
from nozzle_detector import NozzleDetector

# Detetor por simetria radial (nozzle_detector.py), partilhado por todos os
# pedidos: lembra-se da linha da ponta que esta a seguir entre pedidos da mesma
# ferramenta. E' esquecido em reset_nozzle_detector(), chamado quando a
# extensao envia a configuracao (KTAMV_SEND_SERVER_CFG), o que as macros de
# calibracao fazem antes de medir cada ferramenta.
_NOZZLE_DETECTOR = NozzleDetector()
_NOZZLE_DETECTOR_LOCK = threading.Lock()

# Os algoritmos antigos (blob detectors) davam falsos positivos com pontas de
# inox. Ficam desligados: sem detecao, o kTAMV tira outra imagem.
USE_LEGACY_FALLBACK = False

# Centro da imagem (640x480, a resolucao com que o kTAMV trabalha) e distancia
# ate a qual o bico se considera centrado (ver recursively_find_nozzle_position).
CENTER_U, CENTER_V = 320, 240
CENTER_SNAP_PX = 0.5


# Registo do que o detetor faz (para analisar testes de repeticao). Uma linha
# por acontecimento: RESET (nova ferramenta) ou RESULT (posicao devolvida ao
# kTAMV, linha fixada, imagens usadas, se foi arredondada ao centro).
DETECTOR_LOG = os.path.expanduser("~/ktamv_detector.log")


def detector_log(event, **kv):
    try:
        with open(DETECTOR_LOG, "a") as f:
            f.write("%s;%s;%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event,
                                   ";".join("%s=%s" % item for item in kv.items())))
    except Exception:
        pass


def reset_nozzle_detector():
    # Guarda o raio da linha da ferramenta anterior como referencia para a
    # seguinte (mesmo tipo de ponta): ver NozzleDetector.ref_radius.
    with _NOZZLE_DETECTOR_LOCK:
        lk = _NOZZLE_DETECTOR.locked
        ref = lk[1] if lk else _NOZZLE_DETECTOR.ref_radius
        _NOZZLE_DETECTOR.reset(ref_radius=ref)
    detector_log("RESET", ref_radius=("%.1f" % ref) if ref else "-")

class Ktamv_Server_Detection_Manager:
    _CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    _GAMMA_LUT = np.array([((i / 255.0) ** (1.0 / 1.2)) * 255 for i in np.arange(0, 256)]).astype('uint8')
    uv = [None, None]
    __algorithm = None
    __io = None
    
    ##### Setup functions
    # init function
    def __init__(self, log, camera_url, cloud_url, send_to_cloud = False, *args, **kwargs):
        try:
            self.log = log

            # send calling to log
            self.log('*** calling DetectionManager.__init__')
            
            # Whether to send the images to the cloud after detection.
            self.send_to_cloud = send_to_cloud
            
            # The already initialized io object.
            self.__io = io(log=log, camera_url=camera_url, cloud_url=cloud_url, save_image=False)
            
            # This is the last successful algorithm used by the nozzle detection. Should be reset at tool change. Will have to change.
            self.__algorithm = None

            # TAMV has 2 detectors, one for standard and one for relaxed
            self.createDetectors()
            
            # send exiting to log
            self.log('*** exiting DetectionManager.__init__')
        except Exception as e:
            self.log('*** exception in DetectionManager.__init__: %s' % str(e))
            raise e

    # timeout = 20: If no nozzle found in this time, timeout the function
    # min_matches = 3: Minimum amount of matches to confirm toolhead position after a move
    # xy_tolerance = 1: If the nozzle position is within this tolerance, it's considered a match. 1.0 would be 1 pixel. Only whole numbers are supported.
    # put_frame_func: Function to put the frame into the main program
    # Nitidez da ponta do bico, para encontrar a altura de foco da camara
    # (CALIBRATE_IDEX_CAMERA_FOCUS). Usa um detetor proprio (nao mexe no detetor
    # partilhado da calibracao). Mede a nitidez (energia do gradiente) num disco
    # a volta da ponta: assim so' conta a ponta, e nao outras estruturas que
    # entrem em foco a outras alturas.
    def focus_score(self, frames=3, roi=None):
        # roi = (cx, cy, raio) fixo, para a nitidez ser medida sempre no mesmo
        # sitio ao longo de uma varredura de Z; sem roi, usa a ponta detetada.
        det = NozzleDetector()
        grays, centers, radii = [], [], []
        for _ in range(max(1, int(frames))):
            frame = self.__io.get_single_frame()
            if frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            grays.append(gray)
            try:
                r = det.detect(gray)
            except Exception:
                r = None
            if r is not None:
                centers.append(r['center'])
                radii.append(r['radius'])
        if not grays:
            return None
        h, w = grays[0].shape[:2]
        if roi is not None:
            cx, cy, rad = float(roi[0]), float(roi[1]), float(roi[2])
        elif centers:
            cx, cy = np.median(np.array(centers), axis=0)
            rad = max(40.0, 1.6 * float(np.median(radii)))
        else:
            cx, cy, rad = w / 2.0, h / 2.0, 60.0
        yy, xx = np.ogrid[:h, :w]
        disk = (xx - cx) ** 2 + (yy - cy) ** 2 <= rad * rad
        sharp = []
        for gray in grays:
            g = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0)
            gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
            sharp.append(float(np.mean((gx * gx + gy * gy)[disk])))
        return {'sharpness': float(np.median(sharp)), 'detected': len(centers),
                'frames': len(grays),
                'center': [float(cx), float(cy)] if centers else None,
                'radius': float(np.median(radii)) if radii else None}

    def recursively_find_nozzle_position(self, put_frame_func, min_matches, timeout, xy_tolerance):
        self.log('*** calling recursively_find_nozzle_position')
        start_time = time.time()  # Get the current time
        last_pos = (0,0)
        pos_matches = 0
        pos = None
        streak = []     # posicoes coincidentes consecutivas
        frames_used = 0

        while time.time() - start_time < timeout:
            frame = self.__io.get_single_frame()
            frames_used += 1
            positions, processed_frame = self.nozzleDetection(frame)
            if processed_frame is not None:
                put_frame_func(processed_frame)

            self.log('recursively_find_nozzle_position positions: %s' % str(positions))

            if positions is None or len(positions) == 0:
                # Sem medicao nesta imagem: nao conta, espera um pouco e tenta outra
                time.sleep(0.1)
                continue

            pos = positions
            # Only compare XY position, not radius...
            if abs(pos[0] - last_pos[0]) <= xy_tolerance and abs(pos[1] - last_pos[1]) <= xy_tolerance:
                pos_matches += 1
                streak.append(pos)
                if pos_matches >= min_matches:
                    # Devolve a media das posicoes coincidentes (mais precisa do que so' a ultima)
                    pos = (float(np.mean([p[0] for p in streak])), float(np.mean([p[1] for p in streak])))
                    # A extensao do kTAMV so' da o bico por centrado quando o offset
                    # calculado e' EXATAMENTE 0.000 mm. Com centros sub-pixel isso
                    # nunca acontecia (pedia movimentos de 1 um, abaixo do micropasso,
                    # ate esgotar as tentativas). A menos de meio pixel do centro da
                    # imagem devolve-se o centro exato: e' o mesmo criterio que o kTAMV
                    # tinha com centros inteiros, e meio pixel (~8 um) ja e' menos do
                    # que o motor consegue mover (micropasso de 6-12 um).
                    snapped = abs(pos[0] - CENTER_U) < CENTER_SNAP_PX and abs(pos[1] - CENTER_V) < CENTER_SNAP_PX
                    raw = pos
                    if snapped:
                        pos = (float(CENTER_U), float(CENTER_V))
                    lk = _NOZZLE_DETECTOR.locked
                    detector_log("RESULT", u="%.3f" % raw[0], v="%.3f" % raw[1], snapped=int(snapped),
                                 line=("%s%.1f" % ({-1: "vale", 1: "crista", -2: "borda-", 2: "borda+"}.get(lk[0], "?"), lk[1])) if lk else "-",
                                 frames=frames_used)
                    self.log("recursively_find_nozzle_position found %i matches and returning" % pos_matches)
                    # Send the frame and detection to the cloud if enabled.
                    if self.send_to_cloud:
                        self.__io.send_frame_to_cloud(frame, pos, self.__algorithm)
                    break
            else:
                self.log("Position found does not match last position. Last position: %s, current position: %s" % (str(last_pos), str(pos)))   
                self.log("Difference: X%.3f Y%.3f" % (abs(pos[0] - last_pos[0]), abs(pos[1] - last_pos[1])))
                pos_matches = 0
                streak = [pos]

            last_pos = pos
            # Wait 0.3 to leave time for the webcam server to catch up
            # Crowsnest usually caches 0.3 seconds of frames
            time.sleep(0.3)

        self.log("recursively_find_nozzle_position found: %s" % str(last_pos))
        self.log('*** exiting recursively_find_nozzle_position')
        return pos

    def get_preview_frame(self, put_frame_func):
        # self.log('*** calling get_preview_frame')

        frame = self.__io.get_single_frame()
        _, processed_frame = self.nozzleDetection(frame)
        if processed_frame is not None:
            put_frame_func(processed_frame)

        # self.log('*** exiting get_preview_frame')
        return

# ----------------- TAMV Nozzle Detection as tested in ktamv_cv -----------------

    def createDetectors(self):
        # Standard Parameters
        if(True):
            self.standardParams = cv2.SimpleBlobDetector_Params()
            # Thresholds
            self.standardParams.minThreshold = 1
            self.standardParams.maxThreshold = 50
            self.standardParams.thresholdStep = 1
            # Area
            self.standardParams.filterByArea = True
            self.standardParams.minArea = 400
            self.standardParams.maxArea = 900
            # Circularity
            self.standardParams.filterByCircularity = True
            self.standardParams.minCircularity = 0.8
            self.standardParams.maxCircularity= 1
            # Convexity
            self.standardParams.filterByConvexity = True
            self.standardParams.minConvexity = 0.3
            self.standardParams.maxConvexity = 1
            # Inertia
            self.standardParams.filterByInertia = True
            self.standardParams.minInertiaRatio = 0.3

        # Relaxed Parameters
        if(True):
            self.relaxedParams = cv2.SimpleBlobDetector_Params()
            # Thresholds
            self.relaxedParams.minThreshold = 1
            self.relaxedParams.maxThreshold = 50
            self.relaxedParams.thresholdStep = 1
            # Area
            self.relaxedParams.filterByArea = True
            self.relaxedParams.minArea = 600
            self.relaxedParams.maxArea = 15000
            # Circularity
            self.relaxedParams.filterByCircularity = True
            self.relaxedParams.minCircularity = 0.6
            self.relaxedParams.maxCircularity= 1
            # Convexity
            self.relaxedParams.filterByConvexity = True
            self.relaxedParams.minConvexity = 0.1
            self.relaxedParams.maxConvexity = 1
            # Inertia
            self.relaxedParams.filterByInertia = True
            self.relaxedParams.minInertiaRatio = 0.3

        # Super Relaxed Parameters
            t1=20
            t2=200
            all=0.5
            area=200
            
            self.superRelaxedParams = cv2.SimpleBlobDetector_Params()
        
            self.superRelaxedParams.minThreshold = t1
            self.superRelaxedParams.maxThreshold = t2
            
            self.superRelaxedParams.filterByArea = True
            self.superRelaxedParams.minArea = area
            
            self.superRelaxedParams.filterByCircularity = True
            self.superRelaxedParams.minCircularity = all
            
            self.superRelaxedParams.filterByConvexity = True
            self.superRelaxedParams.minConvexity = all
            
            self.superRelaxedParams.filterByInertia = True
            self.superRelaxedParams.minInertiaRatio = all
            
            self.superRelaxedParams.filterByColor = False

            self.superRelaxedParams.minDistBetweenBlobs = 2
            
        # Create 3 detectors
        self.detector = cv2.SimpleBlobDetector_create(self.standardParams)
        self.relaxedDetector = cv2.SimpleBlobDetector_create(self.relaxedParams)
        self.superRelaxedDetector = cv2.SimpleBlobDetector_create(self.superRelaxedParams)


# ----------------- Deteção baseada em foco -----------------
# A ponta do nozzle é a única zona focada da imagem. Medimos a nitidez local
# (variância do Laplaciano), isolamos essa zona e procuramos o orifício só lá dentro.

    @staticmethod
    def sharpness_map(gray, k=15):
        """Mapa de nitidez: variância local do Laplaciano (alto = focado)."""
        g = cv2.GaussianBlur(gray.astype(np.float32), (3, 3), 0)
        lap = cv2.Laplacian(g, cv2.CV_32F, ksize=3)
        mean = cv2.blur(lap, (k, k))
        mean_sq = cv2.blur(lap * lap, (k, k))
        return np.sqrt(np.maximum(mean_sq - mean * mean, 0))

    def focus_mask(self, gray, rel_thresh=0.4, min_peak=6.0):
        """Máscara da região mais focada (a ponta do nozzle). Devolve (mask, bbox) ou (None, None)."""
        s = cv2.GaussianBlur(self.sharpness_map(gray), (0, 0), 5)
        _, maxv, _, maxloc = cv2.minMaxLoc(s)
        if maxv < min_peak:                       # imagem toda desfocada
            return None, None
        mask = (s > rel_thresh * maxv).astype(np.uint8) * 255
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask)
        i = lab[maxloc[1], maxloc[0]]             # só a componente que contém o pico
        mask = np.where(lab == i, 255, 0).astype(np.uint8)
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
        x, y, w, h, _ = stats[i]
        return mask, (x, y, w, h)

    @staticmethod
    def _best_hole(bw, min_r, max_r):
        """Na imagem binaria (anel claro = branco) escolhe o furo redondo que e' o orificio."""
        cnts, hier = cv2.findContours(bw, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        if hier is None:
            return None
        rc = np.array([bw.shape[1] / 2, bw.shape[0] / 2])
        cands = []
        for c, hh in zip(cnts, hier[0]):
            if hh[3] < 0 or len(c) < 8:           # so' "buracos" (contornos filhos)
                continue
            hull = cv2.convexHull(c)              # o contorno do furo e' serrilhado -> usar o hull
            area = cv2.contourArea(hull)
            per = cv2.arcLength(hull, True)
            if per == 0 or 4 * np.pi * area / per ** 2 < 0.75:
                continue
            (cx, cy), r = cv2.minEnclosingCircle(hull)
            if not (min_r <= r <= max_r):
                continue
            dist = np.hypot(cx - rc[0], cy - rc[1])
            cands.append((area / (1.0 + dist / 10.0), cx, cy, r))
        if not cands:
            return None
        # candidato principal = maior furo redondo perto do centro do ROI;
        # depois fica-se com o mais interior dos que lhe sao concentricos (o orificio)
        top = max(cands, key=lambda c: c[0])
        conc = [c for c in cands if np.hypot(c[1] - top[1], c[2] - top[2]) < 0.4 * top[3]]
        return min(conc, key=lambda c: c[3])[1:]

    @staticmethod
    def find_nozzle_hole(gray, bbox, pad=30, min_r=5, max_r=30, max_roi=160):
        """
        Procura o orificio (furo escuro dentro do anel claro) num ROI quadrado centrado
        na zona focada. O ROI e' quadrado e com folga porque o ponto mais nitido costuma
        ser o reflexo num dos lados da ponta, e nao o centro dela.
        Usa threshold ADAPTATIVO: o anel tem brilho desigual (reflexo de um lado) e um
        threshold global parte-o; o adaptativo fecha-o com qualquer exposicao.
        """
        H, W = gray.shape
        x, y, w, h = bbox
        if w > max_roi or h > max_roi:            # zona focada grande demais: nao e' a ponta do nozzle
            return None
        cx, cy = x + w / 2.0, y + h / 2.0
        half = max(w, h) / 2.0 + pad
        x0, y0 = int(max(cx - half, 0)), int(max(cy - half, 0))
        x1, y1 = int(min(cx + half, W)), int(min(cy + half, H))
        roi = Ktamv_Server_Detection_Manager._CLAHE.apply(gray[y0:y1, x0:x1])
        roi = cv2.GaussianBlur(roi, (5, 5), 1.5)
        # varias combinacoes de threshold adaptativo -> mediana: centro estavel entre frames
        res = []
        for block in (21, 31):
            for c in (-1, -2, -3):
                bw = cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY, block, c)
                r = Ktamv_Server_Detection_Manager._best_hole(bw, min_r, max_r)
                if r is not None:
                    res.append(r)
        if len(res) < 3:
            return None
        cx, cy, r = np.median(np.array(res), axis=0)
        return (cx + x0, cy + y0, r)

    def focusedNozzleDetection(self, gray):
        # Detetor por simetria radial. So' aceita medicoes finas (linha do anel).
        # Nunca deixar uma excecao aqui partir o preview ou a calibracao.
        try:
            with _NOZZLE_DETECTOR_LOCK:
                r = _NOZZLE_DETECTOR.detect(gray)
            if r is None or r.get('method') != 'line':
                return None, None
            return (r['center'][0], r['center'][1], r['radius']), None
        except Exception as e:
            self.log('*** exception in focusedNozzleDetection: %s' % str(e))
            return None, None

    @staticmethod
    def apply_focus_mask(img, mask, fill=255):
        """Pinta tudo o que está fora da zona focada com uma cor lisa (não gera blobs)."""
        if mask is None:
            return img
        out = img.copy()
        out[mask == 0] = fill
        return out

    def nozzleDetection(self, image):
        t_start = time.time()
        # working frame object
        nozzleDetectFrame = copy.deepcopy(image)
        # return value for keypoints
        keypoints = None
        center = (None, None)

        # 0) Deteção por foco: se encontrar o orifício, usa-se diretamente.
        gray = cv2.cvtColor(nozzleDetectFrame, cv2.COLOR_BGR2GRAY)
        focus_result, focus_mask = self.focusedNozzleDetection(gray)
        if focus_result is not None:
            fx, fy, fr = focus_result
            keypoints = [cv2.KeyPoint(float(fx), float(fy), float(2 * fr))]
            keypointColor = (255, 0, 255)
            self.__algorithm = 0

        # check which algorithm worked previously
        if keypoints is None and USE_LEGACY_FALLBACK:
            # Os detetores antigos passam a ver só a zona focada
            preprocessorImage0 = self.apply_focus_mask(self.preprocessImage(frameInput=nozzleDetectFrame, algorithm=0), focus_mask)
            preprocessorImage1 = self.apply_focus_mask(self.preprocessImage(frameInput=nozzleDetectFrame, algorithm=1), focus_mask)
            preprocessorImage2 = self.apply_focus_mask(self.preprocessImage(frameInput=nozzleDetectFrame, algorithm=2), focus_mask)

            # apply combo 1 (standard detector, preprocessor 0)
            keypoints = self.detector.detect(preprocessorImage0)
            keypointColor = (0,0,255)
            if(len(keypoints) != 1):
                # apply combo 2 (standard detector, preprocessor 1)
                keypoints = self.detector.detect(preprocessorImage1)
                keypointColor = (0,255,0)
                if(len(keypoints) != 1):
                    # apply combo 3 (relaxed detector, preprocessor 0)
                    keypoints = self.relaxedDetector.detect(preprocessorImage0)
                    keypointColor = (255,0,0)
                    if(len(keypoints) != 1):
                        # apply combo 4 (relaxed detector, preprocessor 1)
                        keypoints = self.relaxedDetector.detect(preprocessorImage1)
                        keypointColor = (39,127,255)

                        if(len(keypoints) != 1):
                            # apply combo 5 (superrelaxed detector, preprocessor 2)
                            keypoints = self.superRelaxedDetector.detect(preprocessorImage2)
                            keypointColor = (39,255,127)
                            if(len(keypoints) != 1):
                                # failed to detect a nozzle, correct return value object
                                keypoints = None
                            else:
                                self.__algorithm = 5
                        else:
                            self.__algorithm = 4
                    else:
                        self.__algorithm = 3
                else:
                    self.__algorithm = 2
            else:
                self.__algorithm = 1
            
        if keypoints is not None:
            self.log("Nozzle detected %i circles with algorithm: %s (%.0f ms)" % (len(keypoints), str(self.__algorithm), (time.time() - t_start) * 1000))
        else:
            self.log("Nozzle detection failed. (%.0f ms)" % ((time.time() - t_start) * 1000))
            
            
        # process keypoint
        if(keypoints is not None and len(keypoints) >= 1):
            # If multiple keypoints are found,
            if len(keypoints) > 1:
                # use the one closest to the center of the image.
                closest_index = self.find_closest_keypoint(keypoints)
            else:
                closest_index = 0
            kp = keypoints[closest_index]
            # Centro com precisao sub-pixel (antes era arredondado ao pixel);
            # o desenho usa a posicao arredondada.
            center = (float(kp.pt[0]), float(kp.pt[1]))
            x, y = int(round(kp.pt[0])), int(round(kp.pt[1]))
            # create radius object
            keypointRadius = np.around(kp.size/2)
            keypointRadius = int(keypointRadius)
            circleFrame = cv2.circle(img=nozzleDetectFrame, center=(x, y), radius=keypointRadius,color=keypointColor,thickness=-1,lineType=cv2.LINE_AA)
            nozzleDetectFrame = cv2.addWeighted(circleFrame, 0.4, nozzleDetectFrame, 0.6, 0)
            nozzleDetectFrame = cv2.circle(img=nozzleDetectFrame, center=(x, y), radius=keypointRadius, color=(0,0,0), thickness=1,lineType=cv2.LINE_AA)
            nozzleDetectFrame = cv2.line(nozzleDetectFrame, (x-5,y), (x+5, y), (255,255,255), 2)
            nozzleDetectFrame = cv2.line(nozzleDetectFrame, (x,y-5), (x, y+5), (255,255,255), 2)
        else:
            # no keypoints, draw a 3 outline circle in the middle of the frame
            keypointRadius = 17
            nozzleDetectFrame = cv2.circle(img=nozzleDetectFrame, center=(320,240), radius=keypointRadius, color=(0,0,0), thickness=3,lineType=cv2.LINE_AA)
            nozzleDetectFrame = cv2.circle(img=nozzleDetectFrame, center=(320,240), radius=keypointRadius+1, color=(0,0,255), thickness=1,lineType=cv2.LINE_AA)
            center = None
        # draw crosshair
        nozzleDetectFrame = cv2.line(nozzleDetectFrame, (320,0), (320,480), (0,0,0), 2)
        nozzleDetectFrame = cv2.line(nozzleDetectFrame, (0,240), (640,240), (0,0,0), 2)
        nozzleDetectFrame = cv2.line(nozzleDetectFrame, (320,0), (320,480), (255,255,255), 1)
        nozzleDetectFrame = cv2.line(nozzleDetectFrame, (0,240), (640,240), (255,255,255), 1)

        # return(center, nozzleDetectFrame)
        return(center, nozzleDetectFrame)

    # Image detection preprocessors
    def preprocessImage(self, frameInput, algorithm=0):
        try:
            outputFrame = self.adjust_gamma(image=frameInput, gamma=1.2)
            height, width, channels = outputFrame.shape
        except: outputFrame = copy.deepcopy(frameInput)
        if(algorithm == 0):
            yuv = cv2.cvtColor(outputFrame, cv2.COLOR_BGR2YUV)
            yuvPlanes = cv2.split(yuv)
            yuvPlanes_0 = cv2.GaussianBlur(yuvPlanes[0],(7,7),6)
            yuvPlanes_0 = cv2.adaptiveThreshold(yuvPlanes_0,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,35,1)
            outputFrame = cv2.cvtColor(yuvPlanes_0,cv2.COLOR_GRAY2BGR)
        elif(algorithm == 1):
            outputFrame = cv2.cvtColor(outputFrame, cv2.COLOR_BGR2GRAY )
            thr_val, outputFrame = cv2.threshold(outputFrame, 127, 255, cv2.THRESH_BINARY|cv2.THRESH_TRIANGLE )
            outputFrame = cv2.GaussianBlur( outputFrame, (7,7), 6 )
            outputFrame = cv2.cvtColor( outputFrame, cv2.COLOR_GRAY2BGR )
        elif(algorithm == 2):
            gray = cv2.cvtColor(frameInput, cv2.COLOR_BGR2GRAY)
            outputFrame = cv2.medianBlur(gray, 5)

        return(outputFrame)

    @staticmethod
    def find_closest_keypoint(keypoints):
        closest_index = None
        closest_distance = float('inf')
        target_point = np.array([320, 240])

        for i, keypoint in enumerate(keypoints):
            point = np.array(keypoint.pt)
            distance = np.linalg.norm(point - target_point)

            if distance < closest_distance:
                closest_distance = distance
                closest_index = i

        return closest_index

    def adjust_gamma(self, image, gamma=1.2):
        # build a lookup table mapping the pixel values [0, 255] to
        # their adjusted gamma values
        if gamma == 1.2:
            return cv2.LUT(image, self._GAMMA_LUT)
        invGamma = 1.0 / gamma
        table = np.array([((i / 255.0) ** invGamma) * 255
            for i in np.arange(0, 256)]).astype( 'uint8' )
        # apply gamma correction using the lookup table
        return cv2.LUT(image, table)

