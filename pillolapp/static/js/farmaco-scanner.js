/**
 * farmaco-scanner.js v4
 * 
 * Strategia semplificata e robusta:
 * 1. BarcodeDetector nativo iOS/Android (se disponibile)
 * 2. ZXing via CDN (fallback)
 * 3. Tesseract OCR (ultimo fallback)
 */

class FarmacoScanner {
  constructor({ videoEl, canvasEl, statusEl, viewfinderEl, onFound }) {
    this.video      = videoEl;
    this.canvas     = canvasEl;
    this.status     = statusEl;
    this.viewfinder = viewfinderEl;
    this.onFound    = onFound;
    this.stream     = null;
    this.running    = false;
    this._rafId     = null;
    this._timer     = null;
    this._detector  = null;
  }

  async start() {
    if (this.running) return;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } }
      });
      this.video.srcObject = this.stream;
      this.video.setAttribute('playsinline', true);

      // Aspetta che il video sia pronto prima di iniziare
      await new Promise((resolve, reject) => {
        this.video.onloadedmetadata = resolve;
        this.video.onerror = reject;
        setTimeout(resolve, 3000); // timeout sicurezza
      });
      await this.video.play();

      // Piccola pausa per stabilizzare il video
      await new Promise(r => setTimeout(r, 500));

      this.running = true;
      if (this.viewfinder) this.viewfinder.classList.remove('hidden');

      if ('BarcodeDetector' in window) {
        this._setStatus('📷 Avvicina il barcode...');
        this._avviaNativo();
      } else {
        this._setStatus('📷 Avvicina la fustella...');
        this._avviaOCR();
      }
    } catch (e) {
      this._setStatus('❌ Camera non disponibile');
      console.error('[Scanner]', e);
    }
  }

  stop() {
    this.running = false;
    if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this.stream) { this.stream.getTracks().forEach(t => t.stop()); this.stream = null; }
    if (this.viewfinder) this.viewfinder.classList.add('hidden');
    this._setStatus('📷 Tocca per scansionare');
  }

  // ── BarcodeDetector nativo ────────────────────────────────────────────

  async _avviaNativo() {
    try {
      const formati = await BarcodeDetector.getSupportedFormats().catch(() => []);
      this._detector = new BarcodeDetector({
        formats: formati.length ? formati : ['code_39','code_128','ean_13','data_matrix','qr_code']
      });
    } catch(e) {
      console.warn('[Scanner] BarcodeDetector init fallito:', e);
      this._avviaOCR();
      return;
    }

    let tentativi = 0;
    const scan = async () => {
      if (!this.running) return;
      if (!this.video || this.video.readyState < 3) {
        this._rafId = requestAnimationFrame(scan);
        return;
      }

      try {
        const barcodes = await this._detector.detect(this.video);
        if (barcodes && barcodes.length > 0) {
          for (const bc of barcodes) {
            const raw = bc.rawValue || '';
            console.log('[Scanner] Barcode:', bc.format, raw);
            const aic = this._estraiAIC(raw);
            if (aic) { this._trovato(aic, bc.format); return; }
          }
        }
      } catch(e) {
        // normale se nessun barcode nel frame
      }

      tentativi++;
      // Dopo 8 secondi senza barcode → prova OCR in parallelo
      if (tentativi === 80) {
        this._setStatus('🔍 Provo lettura testo...');
        this._avviaOCR();
      }
      this._rafId = requestAnimationFrame(scan);
    };
    this._rafId = requestAnimationFrame(scan);
  }

  // ── OCR con Tesseract ─────────────────────────────────────────────────

  _avviaOCR() {
    if (typeof Tesseract === 'undefined') {
      this._setStatus('⚠️ Inserisci AIC manualmente');
      return;
    }
    this._scansionaOCR();
  }

  async _scansionaOCR() {
    if (!this.running) return;
    if (!this.video || this.video.readyState < 2) {
      this._timer = setTimeout(() => this._scansionaOCR(), 800);
      return;
    }

    // Cattura frame
    const ctx = this._getCtx();
    if (!ctx) { this._timer = setTimeout(() => this._scansionaOCR(), 800); return; }
    ctx.drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);

    // Preprocessing: contrasto aumentato
    const imgData = ctx.getImageData(0, 0, this.canvas.width, this.canvas.height);
    this._preprocessing(imgData);
    ctx.putImageData(imgData, 0, 0);

    this._setStatus('🔍 Analisi testo...');

    try {
      const { data } = await Tesseract.recognize(this.canvas, 'ita+eng', {
        logger: () => {},
        tessedit_char_whitelist: 'AIC0123456789abcdefghijklmnopqrstuvwxyz.: ',
      });

      const testo = data.text || '';
      console.log('[Scanner] OCR testo:', testo.substring(0, 100));

      // Pattern in ordine di priorità
      const patterns = [
        /A\.?I\.?C\.?\s*[:\-]?\s*0?(\d{6,9})/i,  // A.I.C.: 033656430
        /\bAIC\s*[:\-]?\s*0?(\d{6,9})/i,           // AIC 033656430
        /\bA0?(\d{8})\b/,                            // A033656430
        /\b(0\d{8})\b/,                              // 033656430 (9 cifre)
        /\b(0\d{5})\b/,                              // 033656 (6 cifre)
      ];

      for (const pat of patterns) {
        const m = testo.match(pat);
        if (m) {
          const aic = m[1].replace(/[^0-9]/g, '');
          if (aic.length >= 6) { this._trovato(aic, 'ocr'); return; }
        }
      }

      this._setStatus('🔍 Non trovato — riposiziona');
    } catch(e) {
      console.warn('[Scanner] OCR errore:', e);
      this._setStatus('🔍 Errore analisi — riprova');
    }

    if (this.running) {
      this._timer = setTimeout(() => this._scansionaOCR(), 2500);
    }
  }

  // ── Utils ─────────────────────────────────────────────────────────────

  _estraiAIC(raw) {
    if (!raw) return null;
    const solo = raw.replace(/[^0-9A-Za-z]/g, '');
    // Farmacode italiano: inizia con A seguito da 9 cifre
    let m = solo.match(/^A(\d{9})$/);
    if (m) return m[1];
    // Solo cifre 9 o 6
    m = solo.match(/^(\d{9})$/);
    if (m) return m[1];
    m = solo.match(/^(\d{6})$/);
    if (m) return m[1];
    // EAN-13 italiano farmaci: inizia con 80
    m = solo.match(/^(80\d{11})$/);
    if (m) return m[1].substring(2, 11); // estrai AIC
    return null;
  }

  _trovato(aic, fonte) {
    console.log(`[Scanner] ✅ AIC trovato (${fonte}): ${aic}`);
    this.stop();
    this._setStatus(`✅ Rilevato: ${aic}`);
    if (this.onFound) this.onFound(aic);
  }

  _getCtx() {
    if (!this.canvas || !this.video) return null;
    this.canvas.width  = this.video.videoWidth  || 640;
    this.canvas.height = this.video.videoHeight || 480;
    return this.canvas.getContext('2d', { willReadFrequently: true });
  }

  _preprocessing(imgData) {
    const d = imgData.data;
    for (let i = 0; i < d.length; i += 4) {
      // Scala di grigi
      const g = 0.299 * d[i] + 0.587 * d[i+1] + 0.114 * d[i+2];
      // Sogliatura adattiva semplice
      const v = g > 140 ? 255 : 0;
      d[i] = d[i+1] = d[i+2] = v;
    }
  }

  _setStatus(msg) {
    if (this.status) this.status.textContent = msg;
  }
}

window.FarmacoScanner = FarmacoScanner;
