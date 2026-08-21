"""Diagnostic: WebView2 getUserMedia + SpeechRecognition event lifecycle."""
from __future__ import annotations

import os
import sys
import time
import threading
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

v = dotenv_values(ROOT / ".env")
for k in ("DATABASE_URL", "GEMINI_API_KEY"):
    if v.get(k):
        os.environ[k] = v[k]


PROBE_JS = r"""
(function(){
  window.__AICA_VOICE_LOG = [];
  function L(msg, extra){
    var row = {t: Date.now(), msg: String(msg), extra: extra || null};
    window.__AICA_VOICE_LOG.push(row);
    console.log('[AICA_VOICE]', msg, extra || '');
    return row;
  }
  window.__AICA_VOICE_DUMP = function(){ return JSON.stringify(window.__AICA_VOICE_LOG); };

  return (async function(){
    L('ORIGIN', location.origin);
    L('HREF', location.href);
    L('SR_EXISTS', !!(window.SpeechRecognition || window.webkitSpeechRecognition));
    L('MEDIADEVICES', !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia));

    // permissions.query if available
    try {
      if (navigator.permissions && navigator.permissions.query) {
        var st = await navigator.permissions.query({name:'microphone'});
        L('MIC_PERMISSION_QUERY', st.state);
      } else {
        L('MIC_PERMISSION_QUERY', 'unsupported');
      }
    } catch (e) {
      L('MIC_PERMISSION_QUERY_ERR', String(e && e.message || e));
    }

    // getUserMedia
    var stream = null;
    try {
      L('MIC_REQUESTED', true);
      stream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
      L('MIC_STREAM_CREATED', true);
      var tracks = stream.getAudioTracks();
      L('AUDIO_TRACK_COUNT', tracks.length);
      if (tracks[0]) {
        var tr = tracks[0];
        var settings = {};
        try { settings = tr.getSettings ? tr.getSettings() : {}; } catch(e){}
        L('AUDIO_TRACK', {
          label: tr.label,
          enabled: tr.enabled,
          muted: tr.muted,
          readyState: tr.readyState,
          settings: settings
        });
        L('AUDIO_CAPTURE_STARTED', tr.readyState === 'live' && tr.enabled && !tr.muted);
      }
      // measure audio level briefly with AnalyserNode
      try {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var src = ctx.createMediaStreamSource(stream);
        var analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        var data = new Uint8Array(analyser.frequencyBinCount);
        var peak = 0;
        for (var i=0;i<20;i++){
          analyser.getByteTimeDomainData(data);
          for (var j=0;j<data.length;j++){
            var v = Math.abs(data[j]-128);
            if (v>peak) peak=v;
          }
          await new Promise(r=>setTimeout(r,50));
        }
        L('AUDIO_LEVEL_PEAK', peak);
        L('AUDIO_DETECTED', peak > 2);
        try { await ctx.close(); } catch(e){}
      } catch (e) {
        L('AUDIO_LEVEL_ERR', String(e && e.message || e));
      }
    } catch (e) {
      L('GUM_ERROR_NAME', e && e.name);
      L('GUM_ERROR_MSG', String(e && e.message || e));
      return window.__AICA_VOICE_DUMP();
    }

    // SpeechRecognition lifecycle (keep GUM stream open during SR)
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      L('SR_MISSING', true);
      try { stream.getTracks().forEach(t=>t.stop()); } catch(e){}
      return window.__AICA_VOICE_DUMP();
    }
    var rec = new SR();
    rec.lang = 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    await new Promise(function(resolve){
      var done = false;
      function finish(){ if(done) return; done=true; resolve(); }
      rec.onstart = function(){ L('SPEECH_RECOGNITION_STARTED', true); };
      rec.onaudiostart = function(){ L('SR_AUDIOSTART', true); };
      rec.onsoundstart = function(){ L('SR_SOUNDSTART', true); };
      rec.onspeechstart = function(){ L('SPEECH_DETECTED', true); };
      rec.onresult = function(ev){
        var t='';
        for (var i=ev.resultIndex;i<ev.results.length;i++){
          t += ev.results[i][0].transcript || '';
        }
        L('TRANSCRIPT_RECEIVED', t);
      };
      rec.onspeechend = function(){ L('SR_SPEECHEND', true); };
      rec.onsoundend = function(){ L('SR_SOUNDEND', true); };
      rec.onaudioend = function(){ L('SR_AUDIOEND', true); };
      rec.onend = function(){ L('RECOGNITION_ENDED', true); finish(); };
      rec.onerror = function(ev){ L('ERROR', ev && ev.error); };
      try {
        rec.start();
        L('SR_START_CALLED', true);
      } catch (e) {
        L('SR_START_ERR', String(e && e.message || e));
        finish();
      }
      setTimeout(function(){
        try { rec.stop(); } catch(e){}
        setTimeout(finish, 800);
      }, 4500);
    });
    try { stream.getTracks().forEach(t=>t.stop()); } catch(e){}
    return window.__AICA_VOICE_DUMP();
  })();
})()
"""


def main() -> int:
    port = 18840
    env = os.environ.copy()
    env["AICA_PORT"] = str(port)
    env["AICA_HOST"] = "127.0.0.1"
    env["AICA_DESKTOP"] = "1"
    exe = ROOT / "dist" / "AICA.Engine" / "AICA.Engine.exe"
    p = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            time.sleep(0.2)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                if p.poll() is not None:
                    print("ENGINE_DIED")
                    return 1
        else:
            print("ENGINE_TIMEOUT")
            return 1

        import webview
        from desktop.launcher.webview_desktop import (
            install_webview2_permission_hook,
            webview_user_data_dir,
        )

        install_webview2_permission_hook()
        storage = webview_user_data_dir()
        result = {"dump": None, "err": None}

        window = webview.create_window(
            "AICA Voice Probe",
            url=f"http://127.0.0.1:{port}/login",
            width=900,
            height=700,
        )

        def run_probe():
            time.sleep(1.0)
            try:
                # evaluate_js may not await async; inject then poll
                window.evaluate_js(
                    "window.__AICA_PROBE_PROMISE = (" + PROBE_JS + "); true"
                )
                deadline = time.time() + 12
                dump = None
                while time.time() < deadline:
                    time.sleep(0.5)
                    dump = window.evaluate_js(
                        "window.__AICA_VOICE_DUMP ? window.__AICA_VOICE_DUMP() : null"
                    )
                    if dump and "RECOGNITION_ENDED" in str(dump) or (
                        dump and "GUM_ERROR" in str(dump)
                    ):
                        break
                result["dump"] = dump
            except Exception as e:
                result["err"] = str(e)
            finally:
                try:
                    window.destroy()
                except Exception:
                    pass

        def on_loaded():
            threading.Thread(target=run_probe, daemon=True).start()

        window.events.loaded += on_loaded
        webview.start(private_mode=False, storage_path=str(storage))
        print("DUMP", result.get("dump"))
        print("ERR", result.get("err"))
        return 0
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
