/*
 * Signature / n-parameter solver for the WEB_REMIX player, hosted inside a bare
 * V8 isolate (mini-racer) - no Node, no browser.
 *
 * The player ships its cipher as an obfuscated bytecode VM whose names, array
 * indices and opcodes rotate between builds, so nothing can be hardcoded. We run
 * the whole base.js in this isolate and inject an "eval-portal" into its IIFE
 * scope, so any closure-private function can be called by name.
 *
 * Two ways to solve a stream URL, tried in this order by the Python host:
 *   __decorate(s, n)   drive the player's own media-URL decorator (the function
 *                      that stamps ("alr","yes") on a stream URL) and read the
 *                      signed `s` and transformed `n` back out. Universal: it is
 *                      what the browser itself runs.
 *   __descramble(s, n) locate and replay the sig / n functions statically. Only
 *                      for older player families where the decorator is missing.
 *
 * The host loads this script once per isolate and calls __setup(baseJs) once per
 * player build. Every entry point returns a JSON string: {sig, n} or {error}.
 */

(function () {
  "use strict";

  // ---- browser globals base.js expects (a bare V8 isolate has none) ----
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

  function lastMatch(re, s) {
    var m,
      last = null;
    while ((m = re.exec(s))) last = m;
    return last;
  }

  var valid = function (x, ref) {
    return typeof x === "string" && x.length >= 8 && x !== ref && /^[A-Za-z0-9_-]+$/.test(x);
  };

  // A correctly transformed `n` is a real scramble of the input: about the same
  // length and NOT containing the input verbatim. A wrong candidate's guard branch
  // typically returns `<prefix>+input` (e.g. "undefined" + n), which still looks
  // URL-safe and would win a vote, yielding a URL googlevideo 403s.
  var validN = function (x, ref) {
    return valid(x, ref) && x.indexOf(ref) < 0 && x.length <= ref.length + 12;
  };

  // A descrambled signature is a reorder of the input chars with a few spliced
  // off, i.e. a sub-multiset of the input. Charset-agnostic.
  function isReorderOf(x, ref) {
    if (typeof x !== "string" || x.length < 8 || x === ref || x.length > ref.length) return false;
    var cnt = {};
    for (var i = 0; i < ref.length; i++) cnt[ref[i]] = (cnt[ref[i]] || 0) + 1;
    for (var j = 0; j < x.length; j++) {
      if (!cnt[x[j]]) return false;
      cnt[x[j]]--;
    }
    return x.length >= ref.length - 30;
  }

  // Run base.js and inject the eval-portal right after the reverse/swap/splice
  // helper object (present in every player family) so its closure is the IIFE
  // scope. The swap fingerprint also yields the string-alias array name (ARR)
  // the static n-driver search needs.
  function makePortal(baseJs) {
    var swap = baseJs.match(
      /[A-Za-z0-9$_]+:function\(([A-Za-z0-9$_]+),([A-Za-z0-9$_]+)\)\{var [A-Za-z0-9$_]+=\1\[0\];\1\[0\]=\1\[\2%\1(?:\.length|\[([A-Za-z0-9$_]+)\[\d+\]\])\]/,
    );
    if (!swap) throw new Error("swap helper not found (player structure changed)");
    var ARR = swap[3] || null;
    var at = baseJs.indexOf(swap[0]);
    var head = lastMatch(/[A-Za-z0-9$_]+=\{/g, baseJs.slice(0, at));
    if (!head) throw new Error("helper object not found");
    var objEnd = matchBrace(baseJs, baseJs.indexOf("{", head.index));
    var src =
      baseJs.slice(0, objEnd) +
      ";globalThis.__evp=function(_x){try{return eval(_x)}catch(e){return undefined}};" +
      baseJs.slice(objEnd);
    (0, eval)(src);
    if (typeof globalThis.__evp !== "function") throw new Error("eval-portal did not initialise");
    return { ev: globalThis.__evp, ARR: ARR };
  }

  // The media-URL decorator: NAME=function(url,key,val){url=new CTOR(url,!0);
  // url.set("alr","yes");...}. Match the whole shape rather than the first
  // ("alr","yes") in the file: newer players also stamp it inside a segment-URL
  // builder that sorts earlier, is a `g.`-property (not callable by bare name
  // through the portal) and takes a segment object instead of a URL. The
  // lookbehind rejects such `.`-prefixed property names.
  function decoratorName(js) {
    var m = js.match(
      /(?<![.\w$])([A-Za-z0-9$_]+)=function\((\w+)[^)]*\)\{\2=new [\w.$]+\(\2,\s*!0[,)][^]{0,40}?\.set\("alr","yes"\)/,
    );
    if (m) return m[1];
    // Older families: the first ("alr","yes") sits in the decorator itself.
    var at = js.search(/"alr"\s*,\s*"yes"/);
    if (at < 0) return null;
    var head = lastMatch(/\b([A-Za-z0-9$_]+)=function\(/g, js.slice(0, at));
    return head ? head[1] : null;
  }

  var state = null;

  g.__setup = function (baseJs) {
    var portal = makePortal(baseJs);
    state = { ev: portal.ev, ARR: portal.ARR, baseJs: baseJs, decorator: decoratorName(baseJs) };
    return JSON.stringify({ decorator: state.decorator });
  };

  // Build a URL through the decorator with the raw signature, set `n`, call the
  // one prototype method that is not a plain accessor (the sign step that runs
  // the cipher VM), then read the transformed values back.
  g.__decorate = function (sIn, nIn) {
    if (!state || !state.decorator) return JSON.stringify({ error: "no decorator in this player" });
    var expr =
      "(function(){" +
      "var u=" +
      state.decorator +
      '("https://youtube.com/watch?v=yt-dlp-wins","s",encodeURIComponent(' +
      JSON.stringify(sIn) +
      "));" +
      (nIn ? 'u.set("n",' + JSON.stringify(nIn) + ");" : "") +
      "var p=Object.getPrototypeOf(u),ks=Object.keys(p).concat(Object.getOwnPropertyNames(p));" +
      'for(var i=0;i<ks.length;i++){var k=ks[i];if(["constructor","set","get","clone"].indexOf(k)<0){u[k]();break}}' +
      'var ss=u.get("s");return {sig:ss?decodeURIComponent(ss):null,n:u.get("n")}' +
      "})()";
    var out = state.ev(expr);
    if (!out || typeof out.sig !== "string")
      return JSON.stringify({ error: "decorator could not be driven" });
    return JSON.stringify({ sig: out.sig, n: typeof out.n === "string" ? out.n : null });
  };

  // Static fallback: replay the sig / n functions located by structure. Tries the
  // nested-int family, the array-challenge n-driver and the classic single-arg
  // family. Fails loud (error) rather than ever returning a wrong value.
  g.__descramble = function (sIn, nIn) {
    try {
      var ev = state.ev;
      var ARR = state.ARR;
      var baseJs = state.baseJs;

      // ---------- signature ----------
      var sig = null;
      var sigFn = null;
      // (A) nested-int form: SIGFN(a,b,ECFN(c,d,X.s))
      var sc = baseJs.match(
        /([A-Za-z0-9$_]+)\((\d+),(\d+),([A-Za-z0-9$_]+)\((\d+),(\d+),[A-Za-z0-9$_.]+\.s\)\)/,
      );
      if (sc) {
        var r = ev(
          sc[1] + "(" + sc[2] + "," + sc[3] + "," + sc[4] + "(" + sc[5] + "," + sc[6] + "," +
            JSON.stringify(sIn) + "))",
        );
        if (typeof r === "string" && r.length >= 10) sig = r;
      }
      // (B) classic single-arg decipher FN(s)
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
      if (!sig) return JSON.stringify({ error: "signature: no strategy matched" });

      // ---------- n-challenge ----------
      var nOut = null;
      var splitIdx = ARR ? ev(ARR + '.indexOf("split")') : -1;
      var vote = function (driver, Q, span, minVotes) {
        var counts = {};
        for (var M = 0; M < span; M++) {
          var rn = ev(driver + "(" + M + "," + (Q ^ M) + "," + JSON.stringify(nIn) + ")");
          if (validN(rn, nIn)) counts[rn] = (counts[rn] || 0) + 1;
        }
        var bestK = null,
          bestV = 0;
        for (var k in counts) {
          if (counts[k] > bestV) {
            bestV = counts[k];
            bestK = k;
          }
        }
        return bestV >= minVotes ? bestK : null;
      };
      if (nIn && ARR && typeof splitIdx === "number" && splitIdx >= 0) {
        // (A) live n-driver: brute the small XOR-masked index space.
        var nMatch = baseJs.match(
          new RegExp(
            "z\\[" + ARR + "\\[([A-Za-z0-9$_]+)\\^(\\d+)\\]\\]\\(" + ARR +
              "\\[\\1\\^\\d+\\]\\)\\s*[,;]\\s*(?:var\\s+)?[A-Za-z0-9$_]+=\\[",
          ),
        );
        if (nMatch) {
          var fnDef = lastMatch(/([A-Za-z0-9$_]+)=function\(/g, baseJs.slice(0, nMatch.index));
          if (fnDef) nOut = vote(fnDef[1], splitIdx ^ Number(nMatch[2]), 64, 1);
        }
        // (A2) array-challenge n-driver: the function building a self-referencing
        // opcode array (`w[V^k]=w`); the split access just before it yields Q.
        if (!nOut) {
          var splitRe = new RegExp(
            "[A-Za-z0-9$_]+\\[" + ARR + "\\[([A-Za-z0-9$_]+)\\^(\\d+)\\]\\]\\(" + ARR +
              "\\[([A-Za-z0-9$_]+)\\^(\\d+)\\]\\)",
            "g",
          );
          var tried = {};
          var selfRe = /([A-Za-z0-9$_]+)\[[A-Za-z0-9$_]+\^\d+\]=\1[,;]/g;
          var sr;
          while (!nOut && (sr = selfRe.exec(baseJs))) {
            var def = lastMatch(/([A-Za-z0-9$_]+)=function\(/g, baseJs.slice(0, sr.index));
            if (!def || tried[def[1]]) continue;
            var win = baseJs.slice(Math.max(0, sr.index - 2000), sr.index);
            var acc = null,
              am;
            splitRe.lastIndex = 0;
            while ((am = splitRe.exec(win))) if (am[1] === am[3]) acc = am;
            if (!acc) continue;
            tried[def[1]] = 1;
            // require a real majority so a single spurious hit can't win
            nOut = vote(def[1], splitIdx ^ Number(acc[2]), 256, 3);
          }
        }
      }
      // (B) classic n: anchor on the actual n-application site.
      if (nIn && !nOut) {
        var candN = {};
        var nearestBefore = function (v, end) {
          var w = baseJs.slice(Math.max(0, end - 140), end);
          var specs = [
            [v + "=([A-Za-z0-9$_]+(?:\\[\\d+\\])?)\\(" + v + "[,)]", false],
            [v + "=([A-Za-z0-9$_]+(?:\\[\\d+\\])?)\\(decodeURIComponent\\(" + v + "\\)", true],
          ];
          for (var si = 0; si < specs.length; si++) {
            var last = lastMatch(new RegExp(specs[si][0], "g"), w);
            if (last && last[1] !== sigFn) candN[last[1]] = specs[si][1];
          }
        };
        var mn;
        var reLit = /\.set\(\s*"n"\s*,\s*(\w+)\s*\)/g;
        while ((mn = reLit.exec(baseJs))) nearestBefore(mn[1], mn.index);
        var reAlias = /\[([A-Za-z0-9$_]+)\[(\d+)\]\]\(\1\[(\d+)\],\s*(\w+)\)/g;
        while ((mn = reAlias.exec(baseJs))) {
          if (ev(mn[1] + "[" + mn[3] + "]") === "n" && ev(mn[1] + "[" + mn[2] + "]") === "set")
            nearestBefore(mn[4], mn.index);
        }
        var probe = nIn.length > 1 ? nIn.slice(1) + nIn[0] : nIn + "A";
        for (var fnn in candN) {
          var dec = candN[fnn];
          var callN = function (val) {
            return ev(
              fnn + "(" + (dec ? "decodeURIComponent(" + JSON.stringify(val) + ")" : JSON.stringify(val)) + ")",
            );
          };
          var rN = callN(nIn);
          if (validN(rN, nIn) && callN(probe) !== rN) {
            nOut = rN;
            break;
          }
        }
      }
      if (nIn && !nOut) return JSON.stringify({ error: "n-challenge: no strategy matched" });
      return JSON.stringify({ sig: sig, n: nOut });
    } catch (e) {
      return JSON.stringify({ error: String(e && e.message ? e.message : e) });
    }
  };
})();
