// Manim JSON timeline renderer — reusable library.
//
// Loads a JSON produced by extract.py and plays it inside any <svg>
// element. Coordinate system: Manim native (x: [-7.11, 7.11], y: [-4, 4]).
// SVG viewBox matches that, with Y inverted via the root group's transform.
//
// Public API:
//     const r = ManimRenderer.create(svgElement, { onStatus });
//     r.load(json);   // prepare DOM from the JSON
//     r.play();       // start the timeline
//     r.reset();      // clear and re-build from the last-loaded data
//
// For standalone use (renderer.html), the bottom of this file auto-wires
// the toolbar buttons.

const ManimRenderer = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function create(stage, options) {
    if (!stage) throw new Error('ManimRenderer.create requires an SVG element');
    options = options || {};
    const onStatus = options.onStatus || (() => {});

    if (!stage.getAttribute('viewBox')) {
      stage.setAttribute('viewBox', '-7.111 -4 14.222 8');
    }
    if (!stage.getAttribute('preserveAspectRatio')) {
      stage.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    }

    // Per-instance state (closed over by the functions below)
    let data = null;
    let root = null;
    let elements = {};
    let timers = [];

    function init() {
      stage.innerHTML = '';
      root = document.createElementNS(NS, 'g');
      root.setAttribute('transform', 'scale(1, -1)');
      stage.appendChild(root);
      elements = {};
      clearTimers();
    }

    function clearTimers() {
      timers.forEach(t => clearTimeout(t));
      timers = [];
    }

    function load(json) {
      data = json;
      reset();
      onStatus(
        `${Object.keys(json.mobjects || {}).length} mobjects · ` +
        `${(json.timeline || []).length} events · ` +
        `${((json.total_duration_ms || 0) / 1000).toFixed(1)}s`
      );
    }

    function reset() {
      init();
      if (!data) return;

      const built = {};
      for (const [id, m] of Object.entries(data.mobjects)) {
        const el = buildElement(m);
        if (el) {
          el.setAttribute('data-manim-id', id);
          el.style.opacity = '0';
          built[id] = el;
        }
      }
      elements = built;

      // parent map: child_id → parent_group_id
      const parentOf = {};
      for (const [id, m] of Object.entries(data.mobjects)) {
        if (m.children) {
          for (const cid of m.children) {
            if (built[cid]) parentOf[cid] = id;
          }
        }
      }

      for (const [id, el] of Object.entries(built)) {
        const pid = parentOf[id];
        const parent = pid ? built[pid] : root;
        if (parent) parent.appendChild(el);
      }
    }

    // ─── Element factories ──────────────────────────────────────

    function buildElement(m) {
      switch (m.type) {
        case 'Square':
        case 'Rectangle':
        case 'SurroundingRectangle':
          return buildRect(m);
        case 'Circle':
          return buildCircle(m);
        case 'Text':
        case 'MarkupText':
          return buildText(m);
        case 'Arrow':
        case 'DoubleArrow':
          return buildArrow(m);
        case 'Line':
          return buildLine(m);
        case 'Brace':
          return buildBrace(m);
        case 'VGroup':
        case 'Group':
          return buildGroup(m);
        default:
          console.warn('[renderer] unsupported type:', m.type, m);
          return null;
      }
    }

    function buildRect(m) {
      const el = document.createElementNS(NS, 'rect');
      const w = m.width ?? 0.5;
      const h = m.height ?? 0.5;
      el.setAttribute('x', m.x - w / 2);
      el.setAttribute('y', m.y - h / 2);
      el.setAttribute('width', w);
      el.setAttribute('height', h);
      applyStrokeFill(el, m);
      return el;
    }

    function buildCircle(m) {
      const el = document.createElementNS(NS, 'circle');
      const r = (m.width ?? m.height ?? 0.5) / 2;
      el.setAttribute('cx', m.x);
      el.setAttribute('cy', m.y);
      el.setAttribute('r', r);
      applyStrokeFill(el, m);
      return el;
    }

    function buildText(m) {
      const el = document.createElementNS(NS, 'text');
      el.setAttribute('x', m.x);
      el.setAttribute('y', -m.y);
      el.setAttribute('text-anchor', 'middle');
      el.setAttribute('dominant-baseline', 'central');
      el.setAttribute('transform', 'scale(1, -1)');
      el.setAttribute('fill', m.color || m.fill_color || '#e6edf3');
      el.setAttribute('stroke', 'none');
      el.setAttribute('font-size', manimFontSizeToUnits(m.font_size));
      el.setAttribute('font-family', '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif');
      if ((m.slant || 'NORMAL') !== 'NORMAL') el.setAttribute('font-style', 'italic');
      if ((m.weight || 'NORMAL') !== 'NORMAL') el.setAttribute('font-weight', 'bold');
      el.textContent = m.text || '';
      return el;
    }

    // Manim font_size lives in Manim "points" — typical sane values are
    // 12-32. Our SVG viewBox is 14 units wide, so we scale by 0.0135 to
    // get readable sizes (font_size 16 → 0.216 user units).
    //
    // The LLM occasionally ignores the prompt's font_size guidance and
    // emits values like 80-130, which renders as text larger than the
    // entire viewport. Clamp to MAX_FONT_UNITS so a wild value can't
    // blow up the layout. 0.5 user units ≈ font_size 37 — readable
    // and contained within the visible area.
    const MAX_FONT_UNITS = 0.5;
    function manimFontSizeToUnits(fs) {
      const f = Number(fs) || 24;
      return Math.min(MAX_FONT_UNITS, f * 0.0135);
    }

    function buildArrow(m) {
      ensureArrowMarker(m.stroke_color || '#e6edf3');
      const [sx, sy] = m.start || [m.x, m.y];
      const [ex, ey] = m.end || [m.x, m.y];
      const el = document.createElementNS(NS, 'line');
      el.setAttribute('x1', sx);
      el.setAttribute('y1', sy);
      el.setAttribute('x2', ex);
      el.setAttribute('y2', ey);
      el.setAttribute('stroke', m.stroke_color || '#e6edf3');
      el.setAttribute('stroke-width', manimStrokeToPx(m.stroke_width || 2));
      el.setAttribute('stroke-linecap', 'round');
      el.setAttribute('marker-end', `url(#ap-arrow-head-${(m.stroke_color || 'def').replace('#', '')})`);
      el.setAttribute('fill', 'none');
      return el;
    }

    function ensureArrowMarker(color) {
      const defs = ensureDefs();
      const id = `ap-arrow-head-${color.replace('#', '')}`;
      if (defs.querySelector(`#${CSS.escape(id)}`)) return;
      const marker = document.createElementNS(NS, 'marker');
      marker.setAttribute('id', id);
      marker.setAttribute('viewBox', '0 0 10 10');
      marker.setAttribute('refX', '9');
      marker.setAttribute('refY', '5');
      marker.setAttribute('markerWidth', '6');
      marker.setAttribute('markerHeight', '6');
      marker.setAttribute('orient', 'auto');
      marker.setAttribute('markerUnits', 'strokeWidth');
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
      path.setAttribute('fill', color);
      marker.appendChild(path);
      defs.appendChild(marker);
    }

    function ensureDefs() {
      let defs = stage.querySelector('defs');
      if (!defs) {
        defs = document.createElementNS(NS, 'defs');
        stage.insertBefore(defs, stage.firstChild);
      }
      return defs;
    }

    function buildLine(m) {
      const [sx, sy] = m.start || [m.x - 0.5, m.y];
      const [ex, ey] = m.end || [m.x + 0.5, m.y];
      const el = document.createElementNS(NS, 'line');
      el.setAttribute('x1', sx);
      el.setAttribute('y1', sy);
      el.setAttribute('x2', ex);
      el.setAttribute('y2', ey);
      el.setAttribute('stroke', m.stroke_color || '#e6edf3');
      el.setAttribute('stroke-width', manimStrokeToPx(m.stroke_width || 2));
      el.setAttribute('stroke-linecap', 'round');
      return el;
    }

    function buildBrace(m) {
      const dx = (m.direction && m.direction[0]) || 0;
      const dy = (m.direction && m.direction[1]) || 1;
      const braceColor = m.fill_color || m.stroke_color || '#8b949e';
      const cx = m.x, cy = m.y;
      const w = m.width || 0.3;
      const h = m.height || 0.3;

      const isHorizontal = Math.abs(dy) > Math.abs(dx);
      const sign = isHorizontal ? Math.sign(dy) : Math.sign(dx);

      let d;
      if (isHorizontal) {
        const L = cx - w / 2, R = cx + w / 2;
        const yInner = cy - (h / 2) * sign;
        const yMid = cy;
        const yTip = cy + (h / 2) * sign;
        const cr = Math.min(0.1, w * 0.06);
        d = [
          `M ${L} ${yInner}`,
          `Q ${L} ${yMid}  ${L + 2 * cr} ${yMid}`,
          `L ${cx - cr} ${yMid}`,
          `Q ${cx} ${yMid}  ${cx} ${yTip}`,
          `Q ${cx} ${yMid}  ${cx + cr} ${yMid}`,
          `L ${R - 2 * cr} ${yMid}`,
          `Q ${R} ${yMid}  ${R} ${yInner}`,
        ].join(' ');
      } else {
        const T = cy + h / 2, B = cy - h / 2;
        const xInner = cx - (w / 2) * sign;
        const xMid = cx;
        const xTip = cx + (w / 2) * sign;
        const cr = Math.min(0.1, h * 0.06);
        d = [
          `M ${xInner} ${T}`,
          `Q ${xMid} ${T}  ${xMid} ${T - 2 * cr}`,
          `L ${xMid} ${cy + cr}`,
          `Q ${xMid} ${cy}  ${xTip} ${cy}`,
          `Q ${xMid} ${cy}  ${xMid} ${cy - cr}`,
          `L ${xMid} ${B + 2 * cr}`,
          `Q ${xMid} ${B}  ${xInner} ${B}`,
        ].join(' ');
      }

      const el = document.createElementNS(NS, 'path');
      el.setAttribute('d', d);
      el.setAttribute('stroke', braceColor);
      el.setAttribute('stroke-width', 0.025);
      el.setAttribute('fill', 'none');
      el.setAttribute('stroke-linejoin', 'round');
      el.setAttribute('stroke-linecap', 'round');
      return el;
    }

    function buildGroup(m) {
      const el = document.createElementNS(NS, 'g');
      el.dataset.children = (m.children || []).join(',');
      return el;
    }

    function applyStrokeFill(el, m) {
      el.setAttribute('stroke', m.stroke_color || 'none');
      const sw = (m.stroke_width ?? 0);
      el.setAttribute('stroke-width', manimStrokeToPx(sw));
      if (m.stroke_opacity !== undefined) el.setAttribute('stroke-opacity', m.stroke_opacity);
      if (m.fill_color) {
        el.setAttribute('fill', m.fill_color);
        el.setAttribute('fill-opacity', (m.fill_opacity ?? 0));
      } else {
        el.setAttribute('fill', 'none');
      }
    }

    function manimStrokeToPx(sw) {
      const units = sw * (14.222 / 1920);
      return Math.max(0.004, units);
    }

    // ─── Timeline playback ──────────────────────────────────────

    function play() {
      reset();
      if (!data) return;
      for (const entry of data.timeline) {
        const t = setTimeout(() => runEntry(entry), entry.at);
        timers.push(t);
      }
      const totalT = setTimeout(
        () => onStatus(`done · ${(data.total_duration_ms / 1000).toFixed(1)}s`),
        data.total_duration_ms
      );
      timers.push(totalT);
      onStatus('playing…');
    }

    function runEntry(entry) {
      try {
        dispatch(entry);
      } catch (e) {
        console.error('[renderer] entry failed', entry, e);
      }
    }

    function dispatch(entry) {
      const { action, target, duration } = entry;
      const el = target ? elements[target] : null;
      const dur = duration || 500;

      switch (action) {
        case 'fade_in':
        case 'create':
        case 'grow_from_center':
        case 'grow_from_edge':
        case 'grow_arrow':
        case 'write':
          if (!el) return;
          fadeInWithGrow(el, dur, action, entry);
          fadeInChildren(target, dur, action, entry);
          break;

        case 'fade_out':
        case 'uncreate':
        case 'unwrite':
          if (!el) return;
          transition(el, 'opacity', 0, dur);
          fadeChildrenOpacity(target, 0, dur);
          break;

        case 'apply_method':
          applyMethod(el, entry);
          break;

        case 'transform':
          applyTransform(el, entry);
          break;

        case 'transform_from_copy':
          applyTransformFromCopy(entry);
          break;

        default:
          if (el) transition(el, 'opacity', 1, dur);
      }
    }

    function fadeInWithGrow(el, dur, action) {
      transition(el, 'opacity', 1, dur);
      if (action === 'grow_from_center') {
        el.style.transformOrigin = 'center';
        el.style.transform = 'scale(0)';
        requestAnimationFrame(() => {
          el.style.transition = `transform ${dur}ms cubic-bezier(0.2, 0.8, 0.2, 1)`;
          el.style.transform = 'scale(1)';
        });
      }
    }

    function fadeInChildren(groupId, dur, action, entry) {
      if (!groupId) return;
      const group = data.mobjects[groupId];
      if (!group || !group.children) return;
      for (const cid of group.children) {
        const child = elements[cid];
        if (child) {
          transition(child, 'opacity', 1, dur);
          fadeInChildren(cid, dur, action, entry);
        }
      }
    }

    function fadeChildrenOpacity(groupId, opacity, dur) {
      if (!groupId) return;
      const group = data.mobjects[groupId];
      if (!group || !group.children) return;
      for (const cid of group.children) {
        const child = elements[cid];
        if (child) {
          transition(child, 'opacity', opacity, dur);
          fadeChildrenOpacity(cid, opacity, dur);
        }
      }
    }

    function applyMethod(el, entry) {
      if (!el) return;
      const { start, end, duration } = entry;
      morphToState(el, entry.target, start, end, duration || 500);
    }

    function applyTransform(el, entry) {
      if (!el) return;
      const { end, duration } = entry;
      const startState = data.mobjects[entry.target];
      morphToState(el, entry.target, startState, end, duration || 500);
    }

    function applyTransformFromCopy(entry) {
      const { start, end, duration } = entry;
      if (!start || !end) return;
      const template = { ...start, id: 'tmp_copy' };
      const clone = buildElement(template);
      if (!clone) return;
      clone.style.opacity = '1';
      root.appendChild(clone);
      requestAnimationFrame(() => {
        morphToState(clone, null, start, end, duration || 500);
      });
    }

    function morphToState(el, targetId, startState, endState, dur) {
      if (!endState) return;
      const type = (targetId && data.mobjects[targetId]?.type) || startState?.type;

      el.style.transition = `all ${dur}ms cubic-bezier(0.4, 0, 0.2, 1)`;

      if (type === 'Text' || type === 'MarkupText') {
        if (endState.x !== undefined) el.setAttribute('x', endState.x);
        if (endState.y !== undefined) el.setAttribute('y', -endState.y);
        if (endState.text !== undefined) el.textContent = endState.text;
        if (endState.color) el.setAttribute('fill', endState.color);
        else if (endState.fill_color) el.setAttribute('fill', endState.fill_color);
        if (endState.font_size !== undefined) {
          el.setAttribute('font-size', manimFontSizeToUnits(endState.font_size));
        }
        el.style.opacity = 1;
      } else if (type === 'Square' || type === 'Rectangle' || type === 'SurroundingRectangle') {
        const w = endState.width ?? 0.5;
        const h = endState.height ?? 0.5;
        el.setAttribute('x', endState.x - w / 2);
        el.setAttribute('y', endState.y - h / 2);
        el.setAttribute('width', w);
        el.setAttribute('height', h);
        if (endState.stroke_color) el.setAttribute('stroke', endState.stroke_color);
        if (endState.fill_color) el.setAttribute('fill', endState.fill_color);
        if (endState.fill_opacity !== undefined) el.setAttribute('fill-opacity', endState.fill_opacity);
        if (endState.stroke_opacity !== undefined) el.setAttribute('stroke-opacity', endState.stroke_opacity);
      } else if (type === 'Circle') {
        el.setAttribute('cx', endState.x);
        el.setAttribute('cy', endState.y);
        el.setAttribute('r', (endState.width ?? 0.5) / 2);
        if (endState.stroke_color) el.setAttribute('stroke', endState.stroke_color);
        if (endState.fill_color) el.setAttribute('fill', endState.fill_color);
      } else if (type === 'Arrow' || type === 'Line' || type === 'DoubleArrow') {
        if (endState.start) {
          el.setAttribute('x1', endState.start[0]);
          el.setAttribute('y1', endState.start[1]);
        }
        if (endState.end) {
          el.setAttribute('x2', endState.end[0]);
          el.setAttribute('y2', endState.end[1]);
        }
      }

      if (endState.fill_opacity !== undefined || endState.stroke_opacity !== undefined) {
        const op = Math.max(endState.fill_opacity ?? 0, endState.stroke_opacity ?? 0);
        el.style.opacity = Math.max(op, 0.05);
      }

      if (type === 'VGroup' || type === 'Group') {
        if (startState && endState.x !== undefined && startState.x !== undefined) {
          const dx = endState.x - startState.x;
          const dy = endState.y - startState.y;
          const prevX = parseFloat(el.dataset.tx || '0');
          const prevY = parseFloat(el.dataset.ty || '0');
          const newX = prevX + dx;
          const newY = prevY + dy;
          animateGroupTranslate(el, prevX, prevY, newX, newY, dur);
          el.dataset.tx = String(newX);
          el.dataset.ty = String(newY);
        }
        makeVGroupVisible(targetId, dur);
      }
    }

    function animateGroupTranslate(el, fromX, fromY, toX, toY, dur) {
      const start = performance.now();
      const tick = (now) => {
        const elapsed = now - start;
        const t = Math.min(1, elapsed / Math.max(dur, 1));
        const eased = 1 - Math.pow(1 - t, 3);
        const x = fromX + (toX - fromX) * eased;
        const y = fromY + (toY - fromY) * eased;
        el.setAttribute('transform', `translate(${x} ${y})`);
        if (t < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }

    function makeVGroupVisible(groupId, dur) {
      if (!groupId) return;
      const el = elements[groupId];
      if (el) {
        el.style.transition = `opacity ${dur}ms ease`;
        el.style.opacity = '1';
      }
      const group = data.mobjects[groupId];
      if (!group) return;
      for (const cid of (group.children || [])) {
        const childEl = elements[cid];
        if (childEl) {
          childEl.style.transition = `opacity ${dur}ms ease`;
          childEl.style.opacity = '1';
        }
        const child = data.mobjects[cid];
        if (child && (child.type === 'VGroup' || child.type === 'Group')) {
          makeVGroupVisible(cid, dur);
        }
      }
    }

    function transition(el, prop, value, dur) {
      el.style.transition = `${prop} ${dur}ms cubic-bezier(0.2, 0.8, 0.2, 1)`;
      void el.offsetWidth;
      if (prop === 'opacity') {
        el.style.opacity = value;
      } else {
        el.style[prop] = value;
      }
    }

    return { load, play, reset };
  }

  return { create };
})();

// ─── Standalone page wiring (renderer.html) ────────────────────
// Only runs when a #stage element exists (i.e., the standalone page).
document.addEventListener('DOMContentLoaded', () => {
  const stage = document.getElementById('stage');
  if (!stage) return;

  const setStatus = (text) => {
    const el = document.getElementById('status');
    if (el) el.textContent = text;
  };
  const r = ManimRenderer.create(stage, { onStatus: setStatus });

  const playBtn = document.getElementById('play-btn');
  const restartBtn = document.getElementById('restart-btn');
  const fileInput = document.getElementById('file-input');
  const urlInput = document.getElementById('url-input');

  if (playBtn) playBtn.addEventListener('click', () => r.play());
  if (restartBtn) restartBtn.addEventListener('click', () => { r.reset(); r.play(); });
  if (fileInput) fileInput.addEventListener('change', e => {
    const f = e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = ev => {
      try { r.load(JSON.parse(ev.target.result)); }
      catch (err) { setStatus('JSON parse error: ' + err.message); }
    };
    reader.readAsText(f);
  });
  if (urlInput) urlInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const p = urlInput.value.trim();
      if (p) fetch(p).then(res => res.json()).then(r.load).catch(err => setStatus('fetch failed: ' + err.message));
    }
  });

  // Auto-load if ?json= param was present
  const urlParams = new URLSearchParams(location.search);
  const jsonPath = urlParams.get('json');
  if (jsonPath) {
    fetch(jsonPath).then(res => res.json()).then(r.load).catch(err => setStatus('fetch failed: ' + err.message));
  }
});

// Export for other scripts (e.g., index.html inline code)
window.ManimRenderer = ManimRenderer;
