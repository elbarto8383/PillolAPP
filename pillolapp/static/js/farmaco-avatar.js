/**
 * farmaco-avatar.js
 * Gestisce la visualizzazione immagine confezione farmaco a 3 livelli:
 *   1. Foto caricata dall'utente (immagine_locale)
 *   2. Immagine Open Products Facts (immagine_url)
 *   3. Avatar SVG generato con iniziali + colore deterministico
 *
 * Uso:
 *   <div class="farmaco-avatar" data-farmaco-id="3" data-size="56"></div>
 *   FarmacoAvatar.render();           // renderizza tutti gli elementi .farmaco-avatar
 *   FarmacoAvatar.renderEl(el, info); // renderizza singolo elemento con dati già disponibili
 */

const FarmacoAvatar = (() => {

  // Cache in memoria per evitare fetch ripetuti
  const _cache = {};

  /**
   * Genera un avatar SVG inline con iniziali e colore.
   * @param {string} iniziali  - es. "TAC"
   * @param {string} colore    - es. "#d97706"
   * @param {number} size      - dimensione in px
   * @returns {string} - HTML img con src data:image/svg+xml
   */
  function avatarSVG(iniziali, colore, size = 56) {
    const r = Math.round(size * 0.22);
    const fs = Math.round(size * 0.33);
    // Colore testo: bianco su sfondi scuri, rilevato dalla luminosità
    const hex = colore.replace("#", "");
    const r2 = parseInt(hex.slice(0, 2), 16);
    const g2 = parseInt(hex.slice(2, 4), 16);
    const b2 = parseInt(hex.slice(4, 6), 16);
    const lum = (0.299 * r2 + 0.587 * g2 + 0.114 * b2) / 255;
    const textColor = lum > 0.55 ? "#1f2937" : "#ffffff";

    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <rect width="${size}" height="${size}" rx="${r}" fill="${colore}"/>
      <text x="${size/2}" y="${size/2}" dominant-baseline="central" text-anchor="middle"
        font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"
        font-size="${fs}" font-weight="700" fill="${textColor}">${iniziali}</text>
    </svg>`;

    const b64 = btoa(unescape(encodeURIComponent(svg)));
    return `data:image/svg+xml;base64,${b64}`;
  }

  /**
   * Costruisce l'HTML dell'immagine/avatar per un farmaco.
   * @param {object} info - { immagine_locale, immagine_url, colore_avatar, nome, id }
   * @param {number} size
   * @returns {string} - HTML <img> o <div> con avatar
   */
  function buildHTML(info, size = 56) {
    const { immagine_locale, immagine_url, colore_avatar, nome, id } = info;
    const src = immagine_locale || immagine_url;
    const iniziali = _iniziali(nome || "?");
    const colore   = colore_avatar || "#2563eb";

    const avatarSrc = avatarSVG(iniziali, colore, size);
    const borderR   = Math.round(size * 0.18);

    if (src) {
      // Ha un'immagine reale: mostrala con fallback all'avatar
      return `<img
        src="${src}"
        alt="${nome}"
        width="${size}" height="${size}"
        style="border-radius:${borderR}px;object-fit:cover;display:block;"
        onerror="this.onerror=null;this.src='${avatarSrc}'"
        title="${nome}"
      >`;
    }

    // Solo avatar SVG
    return `<img
      src="${avatarSrc}"
      alt="${nome}"
      width="${size}" height="${size}"
      style="border-radius:${borderR}px;display:block;"
      title="${nome}"
    >`;
  }

  /**
   * Renderizza un singolo elemento DOM con dati già disponibili (no fetch).
   * @param {Element} el
   * @param {object}  info
   */
  function renderEl(el, info) {
    const size = parseInt(el.dataset.size || "56");
    el.innerHTML = buildHTML(info, size);

    // Aggiunge bottone upload se data-upload="true"
    if (el.dataset.upload === "true" && info.id) {
      const btn = document.createElement("button");
      btn.className = "avatar-upload-btn";
      btn.title = "Cambia foto";
      btn.innerHTML = "📷";
      btn.style.cssText =
        "position:absolute;bottom:0;right:0;width:22px;height:22px;" +
        "border-radius:50%;border:none;background:rgba(0,0,0,0.6);color:white;" +
        "font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;";
      btn.onclick = (e) => { e.stopPropagation(); _apriUpload(info.id, el); };
      el.style.position = "relative";
      el.style.display  = "inline-block";
      el.appendChild(btn);
    }
  }

  /**
   * Renderizza tutti gli elementi .farmaco-avatar nel DOM (fetch lazy).
   */
  async function render() {
    const els = document.querySelectorAll(".farmaco-avatar[data-farmaco-id]");
    for (const el of els) {
      const fid = el.dataset.farmacoId;
      if (!fid) continue;

      if (_cache[fid]) {
        renderEl(el, _cache[fid]);
        continue;
      }

      // Inline data: nessun fetch necessario
      if (el.dataset.nome) {
        const info = {
          id:              fid,
          nome:            el.dataset.nome,
          immagine_locale: el.dataset.immagineLocale || null,
          immagine_url:    el.dataset.immagineUrl || null,
          colore_avatar:   el.dataset.coloreAvatar || null,
        };
        _cache[fid] = info;
        renderEl(el, info);
        continue;
      }

      // Fetch dal backend
      try {
        const r = await fetch(`/api/farmaci/${fid}/avatar`);
        const data = await r.json();
        const info = {
          id:              fid,
          nome:            data.nome,
          immagine_locale: null,
          immagine_url:    null,
          colore_avatar:   data.colore,
        };
        _cache[fid] = info;
        renderEl(el, info);
      } catch (e) {
        console.warn("[FarmacoAvatar] Fetch fallito per id", fid, e);
      }
    }
  }

  // ── Upload foto utente ─────────────────────────────────────────────────

  function _apriUpload(farmacoId, containerEl) {
    const input = document.createElement("input");
    input.type   = "file";
    input.accept = "image/*";
    input.capture = "environment";
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("foto", file);
      try {
        const r = await fetch(`/api/farmaci/${farmacoId}/immagine`, {
          method: "POST",
          body:   fd,
        });
        const data = await r.json();
        if (data.immagine_locale) {
          // Aggiorna cache e re-renderizza
          if (_cache[farmacoId]) {
            _cache[farmacoId].immagine_locale = data.immagine_locale;
          }
          // Forza reload immagine
          const img = containerEl.querySelector("img");
          if (img) img.src = data.immagine_locale + "?t=" + Date.now();
          _toast("Foto aggiornata ✓");
        }
      } catch (e) {
        _toast("Errore upload foto");
      }
    };
    input.click();
  }

  function _iniziali(nome) {
    const pulito = nome.replace(/\d.*/g, "").trim();
    const parole = pulito.split(/\s+/);
    if (parole.length >= 2) return parole.slice(0, 3).map(p => p[0].toUpperCase()).join("");
    return pulito.slice(0, 3).toUpperCase() || "?";
  }

  function _toast(msg) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText =
      "position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);" +
      "background:#1f2937;color:white;padding:0.6rem 1.2rem;border-radius:8px;" +
      "font-size:0.9rem;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.2)";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2500);
  }

  // Auto-render al caricamento del DOM
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }

  return { render, renderEl, buildHTML, avatarSVG };

})();
