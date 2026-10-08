// VNC console fidelity: what the browser shows must match the guest framebuffer (no darkening, crisp).
// Ground truth: `virsh screenshot`. Rendered: the noVNC canvas pixels and a screenshot of the page
// (what the user sees, with CSS scaling and anything layered on top). Several viewports / pixel ratios.
// Creates (and deletes) e2e-f-console (Debian 13 cloud image, text console 1280x800) and e2e-f-console-iso
// (netboot.xyz ISO: coloured VGA text mode 720x400), and the ISO e2e-f-netboot.iso if missing.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const PREFIX = process.env.PREFIX || 'e2e-f-';
const VM = `${PREFIX}console`;
const VM_ISO = `${PREFIX}console-iso`;
const ISO_NAME = `${PREFIX}netboot.iso`;
const ISO_URL = 'https://boot.netboot.xyz/ipxe/netboot.xyz.iso';
process.chdir(path.join(__dirname, 'screenshots'));

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, p, body) => {
  const r = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error(`${method} ${p}: ${r.status} ${JSON.stringify(data)}`);
  return data;
};
const until = async (what, check, timeout = 60000, every = 1000) => {
  const end = Date.now() + timeout;
  for (;;) {
    try { const v = await check(); if (v) return v; } catch (e) { /* retry */ }
    if (Date.now() > end) throw new Error(`timed out: ${what}`);
    await sleep(every);
  }
};
const fbShot = (name) => {
  const file = path.join(fs.mkdtempSync('/tmp/e2e-f-fb-'), 'fb.png');
  execFileSync('virsh', ['-c', 'qemu:///system', 'screenshot', name, file], { stdio: 'ignore' });
  return fs.readFileSync(file).toString('base64');   // libvirt writes PNG for QEMU
};

// Pixel statistics computed in a scratch page (decodes PNGs with the browser itself)
const analyse = (page, images) => page.evaluate(async (imgs) => {
  const decode = async (b64) => {
    const img = new Image();
    img.src = b64.startsWith('data:') ? b64 : `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    return ctx.getImageData(0, 0, c.width, c.height);
  };
  const lum = (d) => {
    const out = new Float32Array(d.width * d.height);
    for (let i = 0; i < out.length; i++) out[i] = 0.2126 * d.data[4 * i] + 0.7152 * d.data[4 * i + 1] + 0.0722 * d.data[4 * i + 2];
    return out;
  };
  const [fb, canvas, clip] = await Promise.all([imgs.fb, imgs.canvas, imgs.clip].map(decode));
  const lf = lum(fb); const lc = lum(clip);
  const mean = (l) => l.reduce((a, b) => a + b, 0) / l.length;
  const lit = lf.filter((v) => v > 40).length;
  // "glyph core" brightness: mean of the brightest pixels, as many as the framebuffer has lit pixels
  // (area-normalised). Smooth scaling smears 1px strokes: this drops while the overall mean stays.
  const core = (l, n) => { if (!n) return 0; const s = Float32Array.from(l).sort(); return mean(s.subarray(s.length - n)); };
  let diff = null;
  if (fb.width === canvas.width && fb.height === canvas.height) {
    let sum = 0;
    for (let i = 0; i < fb.data.length; i += 4) sum += Math.abs(fb.data[i] - canvas.data[i]) + Math.abs(fb.data[i + 1] - canvas.data[i + 1]) + Math.abs(fb.data[i + 2] - canvas.data[i + 2]);
    diff = sum / (fb.width * fb.height * 3);
  }
  const n = Math.round(lit * (clip.width * clip.height) / (fb.width * fb.height));
  return {
    fbSize: [fb.width, fb.height], canvasSize: [canvas.width, canvas.height], clipSize: [clip.width, clip.height],
    fbMean: mean(lf), clipMean: mean(lc), fbCore: core(lf, lit), clipCore: core(lc, n), diff,
  };
}, images);

(async () => {
  const failures = [];
  const check = (ok, what) => { log(ok ? 'OK  ' : 'FAIL', what); if (!ok) failures.push(what); };
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const scratch = await browser.newPage();
  let isoCreated = false;
  const created = [];

  try {
    const image = (await api('GET', '/storage/cloud-images')).find((i) => i.distribution === 'debian' && i.version === '13' && i.status === 'ready');
    if (!image) throw new Error('needs a ready Debian 13 cloud image');
    let iso = (await api('GET', '/storage/isos')).find((i) => i.name === ISO_NAME);
    if (!iso) {
      const task = await api('POST', '/storage/isos/download', { url: ISO_URL, name: ISO_NAME });
      await until('ISO download', async () => (await api('GET', '/tasks')).find((t) => t.id === task.id && t.status === 'completed'), 120000);
      iso = (await api('GET', '/storage/isos')).find((i) => i.name === ISO_NAME);
      isoCreated = true;
    }
    created.push(await api('POST', '/vms', {
      name: VM, memory: 1024, vcpu: 1, disk_size: 8, cloud_image_id: image.id, cloudinit_username: 'dev', cloudinit_password: 'test', start: true,
    }));
    created.push(await api('POST', '/vms', { name: VM_ISO, memory: 1024, vcpu: 1, disk_size: 0, iso_path: iso.path, start: true }));
    log('created', created.map((v) => v.name).join(', '));
    await sleep(45000);   // Debian to its login prompt, iPXE to the netboot.xyz menu

    const cases = [
      { vm: created[0], w: 1440, h: 900, dpr: 1 }, { vm: created[0], w: 1920, h: 1080, dpr: 1 }, { vm: created[0], w: 1440, h: 900, dpr: 2 },
      { vm: created[1], w: 1440, h: 900, dpr: 1 }, { vm: created[1], w: 1920, h: 1080, dpr: 2 },
    ];
    for (const c of cases) {
      const tag = `${c.vm.name.replace(PREFIX, '')} ${c.w}x${c.h}@${c.dpr}x`;
      const page = await browser.newPage({ viewport: { width: c.w, height: c.h }, deviceScaleFactor: c.dpr });
      const problems = [];
      page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text().slice(0, 200)); });
      page.on('pageerror', (e) => problems.push(e.message));
      await page.goto(`${BASE}/vms/${c.vm.id}/console`);
      await page.locator('.vnc-screen canvas').waitFor();
      await page.waitForFunction(() => !document.querySelector('.vnc-overlay'), null, { timeout: 30000 });
      await sleep(2500);
      // the guest may change between the two captures (blinking cursor): retry a few times
      let shot = null;
      for (let i = 0; i < 4; i++) {
        const fb = fbShot(c.vm.name);
        const info = await page.evaluate(() => {
          const cv = document.querySelector('.vnc-screen canvas');
          const r = cv.getBoundingClientRect();
          const effects = [];
          for (let e = cv; e; e = e.parentElement) {
            const s = getComputedStyle(e);
            if (s.opacity !== '1' || s.filter !== 'none' || s.mixBlendMode !== 'normal') effects.push(`${e.tagName}.${e.className} opacity=${s.opacity} filter=${s.filter}`);
          }
          const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return { rect: [r.x, r.y, r.width, r.height], rendering: getComputedStyle(cv).imageRendering, effects,
            onTop: top === cv, dpr: window.devicePixelRatio, data: cv.toDataURL('image/png') };
        });
        const clip = (await page.screenshot({ clip: { x: info.rect[0], y: info.rect[1], width: info.rect[2], height: info.rect[3] } })).toString('base64');
        const stats = await analyse(scratch, { fb, canvas: info.data, clip });
        shot = { info, stats };
        if (stats.diff !== null && stats.diff < 0.5) break;
        await sleep(700);
      }
      const { info, stats } = shot;
      const scale = info.rect[2] / stats.fbSize[0];
      log(tag, `fb ${stats.fbSize.join('x')} shown at ${info.rect[2].toFixed(0)}x${info.rect[3].toFixed(0)} css (x${scale.toFixed(3)}, ${info.rendering}),`,
        `mean lum fb ${stats.fbMean.toFixed(2)} / page ${stats.clipMean.toFixed(2)}, glyph core fb ${stats.fbCore.toFixed(1)} / page ${stats.clipCore.toFixed(1)}, canvas diff ${stats.diff && stats.diff.toFixed(3)}`);
      check(stats.diff !== null && stats.diff < 0.5, `${tag}: canvas pixels = guest framebuffer (lossless, no colour change)`);
      check(info.onTop && info.effects.length === 0, `${tag}: nothing over/dimming the canvas ${info.effects.join('; ')}`);
      check(Math.abs(stats.clipMean - stats.fbMean) < Math.max(0.5, stats.fbMean * 0.05), `${tag}: page as bright as the framebuffer overall`);
      const deviceScale = scale * info.dpr;
      if (deviceScale >= 1 - 1e-6) {
        check(info.rendering === 'pixelated', `${tag}: upscaled without smoothing`);
        check(stats.clipCore >= stats.fbCore * 0.95, `${tag}: glyphs keep their brightness (${stats.clipCore.toFixed(1)} vs ${stats.fbCore.toFixed(1)})`);
      } else {
        check(stats.clipCore >= stats.fbCore * 0.7, `${tag}: downscaled glyphs not too dim (${stats.clipCore.toFixed(1)} vs ${stats.fbCore.toFixed(1)})`);
      }
      if (Math.abs(deviceScale - Math.round(deviceScale)) > 0.01 && deviceScale > 1 && deviceScale < 1.15) failures.push(`${tag}: near-1 scale not snapped`);
      await page.screenshot({ path: `console-${c.vm.name.replace(PREFIX, '')}-${c.w}-${c.dpr}x.png` });

      if (c === cases[0]) {
        // 1:1 toggle (sharpest view when the window is smaller than the guest screen), then back to fit
        await page.getByRole('button', { name: '1:1' }).click();
        await sleep(500);
        const native = await page.evaluate(() => { const cv = document.querySelector('.vnc-screen canvas'); const r = cv.getBoundingClientRect(); return [r.width, r.height, cv.width, cv.height, getComputedStyle(cv).imageRendering]; });
        check(native[0] === native[2] && native[1] === native[3] && native[4] === 'pixelated', `1:1 shows the guest screen pixel for pixel (${native.join(' ')})`);
        await page.screenshot({ path: 'console-native.png' });
        await page.reload();
        await page.waitForFunction(() => !document.querySelector('.vnc-overlay'), null, { timeout: 30000 });
        check(await page.getByRole('button', { name: 'Fit' }).isVisible(), '1:1 choice remembered across reloads');
        await page.getByRole('button', { name: 'Fit' }).click();
        await sleep(500);
        const fit = await page.evaluate(() => document.querySelector('.vnc-screen canvas').getBoundingClientRect().width);
        check(Math.abs(fit - info.rect[2]) < 1, 'back to fit');
        // Reconnect, Ctrl+Alt+Del button state, fullscreen
        await page.getByRole('button', { name: 'Reconnect' }).click();
        await sleep(300);
        const cad = page.getByRole('button', { name: 'Ctrl+Alt+Del' });
        check(await until('reconnect', async () => !(await page.locator('.vnc-overlay').count()) && cad.isEnabled(), 30000, 300).catch(() => false),
          'reconnects (Ctrl+Alt+Del enabled again)');
        await page.getByRole('button', { name: 'Fullscreen' }).click();
        await sleep(800);
        const fs1 = await page.evaluate(() => { const el = document.fullscreenElement; const cv = document.querySelector('.vnc-screen canvas').getBoundingClientRect(); return el ? [el.className, cv.width, cv.height] : null; });
        check(fs1 && fs1[0] === 'vnc-screen', `fullscreen shows the console (${fs1})`);
        await page.evaluate(() => document.exitFullscreen && document.fullscreenElement && document.exitFullscreen());
      }
      check(problems.length === 0, `${tag}: no console errors ${problems.join(' | ')}`);
      await page.close();
    }
  } catch (e) {
    failures.push(String(e && e.stack || e));
    log('ERROR', e);
  } finally {
    const vms = await api('GET', '/vms').catch(() => []);
    for (const v of vms.filter((x) => x.name === VM || x.name === VM_ISO)) {
      await api('DELETE', `/vms/${v.id}?delete_disks=true`).catch((e) => log('cleanup', e.message));
    }
    if (isoCreated) {
      const vol = (await api('GET', '/storage/volumes').catch(() => [])).find((v) => v.name === ISO_NAME);
      if (vol) await api('DELETE', `/storage/volumes/${vol.id}`).catch((e) => log('cleanup iso', e.message));
    }
    await browser.close();
  }
  console.log(failures.length ? `\n${failures.length} FAILURE(S):\n- ${failures.join('\n- ')}` : '\nALL OK');
  process.exit(failures.length ? 1 : 0);
})();
