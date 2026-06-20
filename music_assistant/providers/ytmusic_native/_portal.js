/*
 * Structural signature / n-parameter descrambler for the WEB_REMIX player,
 * hosted inside a bare V8 isolate (mini-racer) - no Node, no browser.
 *
 * Ported from the ytmusicnative reverse-engineering effort (reverseengeneer.md
 * §8.1). The current YouTube Music player ships its cipher as an obfuscated
 * bytecode VM whose var names, array indices and opcodes rotate between builds,
 * so nothing can be hardcoded. We run the whole base.js in this isolate, inject
 * an "eval-portal" into the IIFE scope so we can call any closure-private
 * function by name, then derive the sig / n transforms purely from the player's
 * STRUCTURE each run.
 *
 * The Python host loads this script once per isolate, then calls:
 *   __setup(baseJs)      -> runs base.js + wires the portal (once per player)
 *   __descramble(s, n)   -> {sig, n} for one format (n may be null)
 * On failure __descramble returns {error: "..."}, so Python can fall back to
 * the lower-quality ANDROID_VR path.
 */

(function () {
  "use strict";

  // ---- browser globals base.js expects (V8 isolate has none) ----
  var g = globalThis;
  g.window = g;
  g.self = g;
  g.top = g;
  g.navigator = {
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    platform: "Win32",
    languages: ["en"],
    language: "en",
  };
  g.location = {
    href: "https://music.youtube.com/",
    protocol: "https:",
    hostname: "music.youtube.com",
    origin: "https://music.youtube.com",
    search: "",
    hash: "",
    pathname: "/",
  };

  function makeStub() {
    var f = function () {
      return stub;
    };
    var stub = new Proxy(f, {
      get: function (_t, p) {
        return p === "length"
          ? 0
          : p === "toString" || p === Symbol.toPrimitive
            ? function () {
                return "";
              }
            : stub;
      },
      set: function () {
        return true;
      },
      apply: function () {
        return stub;
      },
      construct: function () {
        return stub;
      },
      has: function () {
        return true;
      },
    });
    return stub;
  }
  g.document = makeStub();
  g.XMLHttpRequest = function () {
    return makeStub();
  };
  g.setTimeout = function () {
    return 0;
  };
  g.clearTimeout = function () {};
  g.setInterval = function () {
    return 0;
  };
  g.clearInterval = function () {};
  g.console = {
    log: function () {},
    warn: function () {},
    error: function () {},
    info: function () {},
    debug: function () {},
  };
  g.performance = {
    now: function () {
      return Date.now();
    },
  };

  // ---- polyfills V8-standalone lacks ----
  if (typeof g.TextEncoder === "undefined") {
    g.TextEncoder = function () {};
    g.TextEncoder.prototype.encode = function (str) {
      var utf8 = unescape(encodeURIComponent(String(str)));
      var arr = new Uint8Array(utf8.length);
      for (var i = 0; i < utf8.length; i++) arr[i] = utf8.charCodeAt(i);
      return arr;
    };
  }
  if (typeof g.TextDecoder === "undefined") {
    g.TextDecoder = function () {};
    g.TextDecoder.prototype.decode = function (buf) {
      var bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf || 0);
      var s = "";
      for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      try {
        return decodeURIComponent(escape(s));
      } catch (e) {
        return s;
      }
    };
  }
  if (typeof g.btoa === "undefined") {
    var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    g.btoa = function (s) {
      s = String(s);
      var o = "";
      for (var i = 0; i < s.length; ) {
        var c1 = s.charCodeAt(i++),
          c2 = s.charCodeAt(i++),
          c3 = s.charCodeAt(i++);
        var e1 = c1 >> 2,
          e2 = ((c1 & 3) << 4) | (c2 >> 4),
          e3 = ((c2 & 15) << 2) | (c3 >> 6),
          e4 = c3 & 63;
        if (isNaN(c2)) {
          e3 = e4 = 64;
        } else if (isNaN(c3)) {
          e4 = 64;
        }
        o += B64[e1] + B64[e2] + (e3 === 64 ? "=" : B64[e3]) + (e4 === 64 ? "=" : B64[e4]);
      }
      return o;
    };
    g.atob = function (s) {
      s = String(s).replace(/=+$/, "");
      var o = "",
        bs = 0,
        bits = 0;
      for (var i = 0; i < s.length; i++) {
        var idx = B64.indexOf(s[i]);
        if (idx < 0) continue;
        bs = (bs << 6) | idx;
        bits += 6;
        if (bits >= 8) {
          bits -= 8;
          o += String.fromCharCode((bs >> bits) & 0xff);
        }
      }
      return o;
    };
  }

  // ---- structural helpers ----
  function matchBrace(s, open) {
    var d = 0;
    for (var i = open; i < s.length; i++) {
      if (s[i] === "{") d++;
      else if (s[i] === "}" && !--d) return i + 1;
    }
    return -1;
  }

  var valid = function (x, ref) {
    return (
      typeof x === "string" &&
      x.length >= 8 &&
      x !== ref &&
      /^[A-Za-z0-9_-]+$/.test(x)
    );
  };

  // a descrambled signature is a reorder of the input chars with a few spliced
  // off, i.e. a sub-multiset of the input. Charset-agnostic.
  function isReorderOf(x, ref) {
    if (typeof x !== "string" || x.length < 8 || x === ref || x.length > ref.length)
      return false;
    var cnt = {};
    for (var i = 0; i < ref.length; i++) cnt[ref[i]] = (cnt[ref[i]] || 0) + 1;
    for (var j = 0; j < x.length; j++) {
      if (!cnt[x[j]]) return false;
      cnt[x[j]]--;
    }
    return x.length >= ref.length - 30;
  }

  // Run base.js and inject the eval-portal right after the helper object so its
  // closure is the IIFE scope. The swap fingerprint also yields the string-alias
  // array name (ARR), which the live n-driver needs.
  function makePortal(baseJs) {
    var swap = baseJs.match(
      /[A-Za-z0-9$_]+:function\(([A-Za-z0-9$_]+),([A-Za-z0-9$_]+)\)\{var [A-Za-z0-9$_]+=\1\[0\];\1\[0\]=\1\[\2%\1(?:\.length|\[([A-Za-z0-9$_]+)\[\d+\]\])\]/,
    );
    if (!swap) throw new Error("swap helper not found (player structure changed)");
    var ARR = swap[3] || null;
    var heads = baseJs.slice(0, baseJs.indexOf(swap[0])).match(/[A-Za-z0-9$_]+=\{/g);
    if (!heads || !heads.length) throw new Error("helper object not found");
    var lastHead = heads[heads.length - 1];
    var headIdx = baseJs.lastIndexOf(lastHead, baseJs.indexOf(swap[0]));
    var objEnd = matchBrace(baseJs, baseJs.indexOf("{", headIdx));
    var src =
      baseJs.slice(0, objEnd) +
      ";globalThis.__evp=function(_x){try{return eval(_x)}catch(e){return undefined}};" +
      baseJs.slice(objEnd);
    (0, eval)(src);
    if (typeof globalThis.__evp !== "function")
      throw new Error("eval-portal did not initialise");
    return { ev: globalThis.__evp, ARR: ARR };
  }

  g.__setup = function (baseJs) {
    var portal = makePortal(baseJs);
    g.__EV = portal.ev;
    g.__ARR = portal.ARR;
    g.__BASEJS = baseJs;
    return true;
  };

  // Returns {sig, n} or {error}. Tries the current music family first, then the
  // classic single-arg-decipher family.
  g.__descramble = function (sIn, nIn) {
    try {
      var ev = g.__EV;
      var ARR = g.__ARR;
      var baseJs = g.__BASEJS;

      // ---------- signature ----------
      var sig = null;
      var sigFn = null;
      var sc = baseJs.match(
        /([A-Za-z0-9$_]+)\((\d+),(\d+),([A-Za-z0-9$_]+)\((\d+),(\d+),[A-Za-z0-9$_.]+\.s\)\)/,
      );
      if (sc) {
        var r = ev(
          sc[1] +
            "(" +
            sc[2] +
            "," +
            sc[3] +
            "," +
            sc[4] +
            "(" +
            sc[5] +
            "," +
            sc[6] +
            "," +
            JSON.stringify(sIn) +
            "))",
        );
        if (typeof r === "string" && r.length >= 10) sig = r;
      }
      if (!sig) {
        var cand = {};
        var m;
        var re1 = /([A-Za-z0-9$_]+)\(decodeURIComponent\(/g;
        while ((m = re1.exec(baseJs))) cand[m[1]] = 1;
        var re2 =
          /([A-Za-z0-9$_]+)=function\(\w\)\{\w=\w(?:\.split\(""\)|\[[A-Za-z0-9$_]+\[\d+\]\]\([A-Za-z0-9$_]+\[\d+\]\))/g;
        while ((m = re2.exec(baseJs))) cand[m[1]] = 1;
        for (var fn in cand) {
          var rr = ev(fn + "(" + JSON.stringify(sIn) + ")");
          if (isReorderOf(rr, sIn)) {
            sig = rr;
            sigFn = fn;
            break;
          }
        }
      }
      if (!sig)
        return { error: "signature: no strategy matched (player structure changed)" };

      // ---------- n-challenge ----------
      var nOut = null;
      if (nIn) {
        var nMatch = null;
        if (ARR) {
          nMatch = baseJs.match(
            new RegExp(
              "z\\[" +
                ARR +
                "\\[([A-Za-z0-9$_]+)\\^(\\d+)\\]\\]\\(" +
                ARR +
                "\\[\\1\\^\\d+\\]\\)\\s*[,;]\\s*(?:var\\s+)?[A-Za-z0-9$_]+=\\[",
            ),
          );
        }
        if (nMatch) {
          var splitOff = Number(nMatch[2]);
          var before = baseJs.slice(0, nMatch.index);
          var fnAll = before.match(/([A-Za-z0-9$_]+)=function\(/g);
          var nDriver = null;
          if (fnAll && fnAll.length) {
            nDriver = fnAll[fnAll.length - 1].replace(/=function\($/, "");
          }
          var splitIdx = ev(ARR + '.indexOf("split")');
          if (nDriver && typeof splitIdx === "number" && splitIdx >= 0) {
            var Q = splitIdx ^ splitOff;
            var counts = {};
            for (var M = 0; M < 64; M++) {
              var rn = ev(
                nDriver + "(" + M + "," + (Q ^ M) + "," + JSON.stringify(nIn) + ")",
              );
              if (valid(rn, nIn)) counts[rn] = (counts[rn] || 0) + 1;
            }
            var bestK = null,
              bestV = -1;
            for (var k in counts) {
              if (counts[k] > bestV) {
                bestV = counts[k];
                bestK = k;
              }
            }
            if (bestK !== null) nOut = bestK;
          }
        }
        if (!nOut) {
          var candN = {}; // fn -> dec
          var nearestBefore = function (v, end) {
            var w = baseJs.slice(Math.max(0, end - 140), end);
            var specs = [
              [v + "=([A-Za-z0-9$_]+(?:\\[\\d+\\])?)\\(" + v + "[,)]", false],
              [
                v + "=([A-Za-z0-9$_]+(?:\\[\\d+\\])?)\\(decodeURIComponent\\(" + v + "\\)",
                true,
              ],
            ];
            for (var si = 0; si < specs.length; si++) {
              var re = new RegExp(specs[si][0], "g");
              var last = null,
                mm;
              while ((mm = re.exec(w))) last = mm;
              if (last && last[1] !== sigFn) candN[last[1]] = specs[si][1];
            }
          };
          var mn;
          var reLit = /\.set\(\s*"n"\s*,\s*(\w+)\s*\)/g;
          while ((mn = reLit.exec(baseJs))) nearestBefore(mn[1], mn.index);
          var reAlias = /\[([A-Za-z0-9$_]+)\[(\d+)\]\]\(\1\[(\d+)\],\s*(\w+)\)/g;
          while ((mn = reAlias.exec(baseJs))) {
            var A = mn[1];
            if (ev(A + "[" + mn[3] + "]") === "n" && ev(A + "[" + mn[2] + "]") === "set")
              nearestBefore(mn[4], mn.index);
          }
          var probe = nIn.length > 1 ? nIn.slice(1) + nIn[0] : nIn + "A";
          for (var fnn in candN) {
            var dec = candN[fnn];
            var callN = function (val) {
              return ev(
                fnn +
                  "(" +
                  (dec
                    ? "decodeURIComponent(" + JSON.stringify(val) + ")"
                    : JSON.stringify(val)) +
                  ")",
              );
            };
            var rN = callN(nIn);
            if (
              valid(rN, nIn) &&
              rN.length <= nIn.length + 12 &&
              callN(probe) !== rN
            ) {
              nOut = rN;
              break;
            }
          }
        }
        if (!nOut)
          return { error: "n-challenge: no strategy matched (player structure changed)" };
      }
      return { sig: sig, n: nOut };
    } catch (e) {
      return { error: String(e && e.message ? e.message : e) };
    }
  };
})();
