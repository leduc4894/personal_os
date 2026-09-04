"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __commonJS = (cb, mod) => function __require() {
  try {
    return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
  } catch (e) {
    throw mod = 0, e;
  }
};
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// ../../node_modules/.pnpm/sql.js@1.14.2/node_modules/sql.js/dist/sql-wasm-browser.js
var require_sql_wasm_browser = __commonJS({
  "../../node_modules/.pnpm/sql.js@1.14.2/node_modules/sql.js/dist/sql-wasm-browser.js"(exports, module2) {
    var initSqlJsPromise = void 0;
    var initSqlJs2 = function(moduleConfig) {
      if (initSqlJsPromise) {
        return initSqlJsPromise;
      }
      initSqlJsPromise = new Promise(function(resolveModule, reject) {
        var Module = typeof moduleConfig !== "undefined" ? moduleConfig : {};
        var originalOnAbortFunction = Module["onAbort"];
        Module["onAbort"] = function(errorThatCausedAbort) {
          reject(new Error(errorThatCausedAbort));
          if (originalOnAbortFunction) {
            originalOnAbortFunction(errorThatCausedAbort);
          }
        };
        Module["postRun"] = Module["postRun"] || [];
        Module["postRun"].push(function() {
          resolveModule(Module);
        });
        module2 = void 0;
        var k;
        k ||= typeof Module != "undefined" ? Module : {};
        var aa = !!globalThis.window, ba = !!globalThis.WorkerGlobalScope;
        k.onRuntimeInitialized = function() {
          function a(f, l) {
            switch (typeof l) {
              case "boolean":
                bc(f, l ? 1 : 0);
                break;
              case "number":
                cc(f, l);
                break;
              case "string":
                dc(f, l, -1, -1);
                break;
              case "object":
                if (null === l) eb(f);
                else if (null != l.length) {
                  var n = ca(l.length);
                  m.set(l, n);
                  ec(f, n, l.length, -1);
                  da(n);
                } else ua(f, "Wrong API use : tried to return a value of an unknown type (" + l + ").", -1);
                break;
              default:
                eb(f);
            }
          }
          function b(f, l) {
            for (var n = [], p = 0; p < f; p += 1) {
              var r = t(l + 4 * p, "i32"), v = fc(r);
              if (1 === v || 2 === v) r = gc(r);
              else if (3 === v) r = hc(r);
              else if (4 === v) {
                v = r;
                r = ic(v);
                v = jc(v);
                for (var J = new Uint8Array(r), I2 = 0; I2 < r; I2 += 1) J[I2] = m[v + I2];
                r = J;
              } else r = null;
              n.push(r);
            }
            return n;
          }
          function c(f, l) {
            this.Qa = f;
            this.db = l;
            this.Oa = 1;
            this.yb = [];
          }
          function d(f, l) {
            this.db = l;
            this.ob = ea(f);
            if (null === this.ob) throw Error("Unable to allocate memory for the SQL string");
            this.ub = this.ob;
            this.gb = this.Fb = null;
          }
          function e(f) {
            this.filename = "dbfile_" + (4294967295 * Math.random() >>> 0);
            if (null != f) {
              var l = this.filename, n = "/", p = l;
              n && (n = "string" == typeof n ? n : fa(n), p = l ? ha(n + "/" + l) : n);
              l = ia(true, true);
              p = ja(
                p,
                l
              );
              if (f) {
                if ("string" == typeof f) {
                  n = Array(f.length);
                  for (var r = 0, v = f.length; r < v; ++r) n[r] = f.charCodeAt(r);
                  f = n;
                }
                ka(p, l | 146);
                n = ma(p, 577);
                na(n, f, 0, f.length, 0);
                oa(n);
                ka(p, l);
              }
            }
            this.handleError(q(this.filename, g));
            this.db = t(g, "i32");
            hb(this.db);
            this.pb = {};
            this.Sa = {};
          }
          var g = y(4), h2 = k.cwrap, q = h2("sqlite3_open", "number", ["string", "number"]), w = h2("sqlite3_close_v2", "number", ["number"]), u = h2("sqlite3_exec", "number", ["number", "string", "number", "number", "number"]), x = h2("sqlite3_changes", "number", ["number"]), D = h2(
            "sqlite3_prepare_v2",
            "number",
            ["number", "string", "number", "number", "number"]
          ), ib = h2("sqlite3_sql", "string", ["number"]), lc = h2("sqlite3_normalized_sql", "string", ["number"]), jb = h2("sqlite3_prepare_v2", "number", ["number", "number", "number", "number", "number"]), mc = h2("sqlite3_bind_text", "number", ["number", "number", "number", "number", "number"]), kb = h2("sqlite3_bind_blob", "number", ["number", "number", "number", "number", "number"]), nc = h2("sqlite3_bind_double", "number", ["number", "number", "number"]), oc = h2("sqlite3_bind_int", "number", [
            "number",
            "number",
            "number"
          ]), pc = h2("sqlite3_bind_parameter_index", "number", ["number", "string"]), qc = h2("sqlite3_step", "number", ["number"]), rc = h2("sqlite3_errmsg", "string", ["number"]), sc = h2("sqlite3_column_count", "number", ["number"]), tc = h2("sqlite3_data_count", "number", ["number"]), uc = h2("sqlite3_column_double", "number", ["number", "number"]), lb = h2("sqlite3_column_text", "string", ["number", "number"]), vc = h2("sqlite3_column_blob", "number", ["number", "number"]), wc = h2("sqlite3_column_bytes", "number", ["number", "number"]), xc = h2(
            "sqlite3_column_type",
            "number",
            ["number", "number"]
          ), yc = h2("sqlite3_column_name", "string", ["number", "number"]), zc = h2("sqlite3_reset", "number", ["number"]), Ac = h2("sqlite3_clear_bindings", "number", ["number"]), Bc = h2("sqlite3_finalize", "number", ["number"]), mb = h2("sqlite3_create_function_v2", "number", "number string number number number number number number number".split(" ")), fc = h2("sqlite3_value_type", "number", ["number"]), ic = h2("sqlite3_value_bytes", "number", ["number"]), hc = h2("sqlite3_value_text", "string", ["number"]), jc = h2(
            "sqlite3_value_blob",
            "number",
            ["number"]
          ), gc = h2("sqlite3_value_double", "number", ["number"]), cc = h2("sqlite3_result_double", "", ["number", "number"]), eb = h2("sqlite3_result_null", "", ["number"]), dc = h2("sqlite3_result_text", "", ["number", "string", "number", "number"]), ec = h2("sqlite3_result_blob", "", ["number", "number", "number", "number"]), bc = h2("sqlite3_result_int", "", ["number", "number"]), ua = h2("sqlite3_result_error", "", ["number", "string", "number"]), nb = h2("sqlite3_aggregate_context", "number", ["number", "number"]), hb = h2(
            "RegisterExtensionFunctions",
            "number",
            ["number"]
          ), ob = h2("sqlite3_update_hook", "number", ["number", "number", "number"]);
          c.prototype.bind = function(f) {
            if (!this.Qa) throw "Statement closed";
            this.reset();
            return Array.isArray(f) ? this.Wb(f) : null != f && "object" === typeof f ? this.Xb(f) : true;
          };
          c.prototype.step = function() {
            if (!this.Qa) throw "Statement closed";
            this.Oa = 1;
            var f = qc(this.Qa);
            switch (f) {
              case 100:
                return true;
              case 101:
                return false;
              default:
                throw this.db.handleError(f);
            }
          };
          c.prototype.Pb = function(f) {
            null == f && (f = this.Oa, this.Oa += 1);
            return uc(this.Qa, f);
          };
          c.prototype.hc = function(f) {
            null == f && (f = this.Oa, this.Oa += 1);
            f = lb(this.Qa, f);
            if ("function" !== typeof BigInt) throw Error("BigInt is not supported");
            return BigInt(f);
          };
          c.prototype.mc = function(f) {
            null == f && (f = this.Oa, this.Oa += 1);
            return lb(this.Qa, f);
          };
          c.prototype.getBlob = function(f) {
            null == f && (f = this.Oa, this.Oa += 1);
            var l = wc(this.Qa, f);
            f = vc(this.Qa, f);
            for (var n = new Uint8Array(l), p = 0; p < l; p += 1) n[p] = m[f + p];
            return n;
          };
          c.prototype.get = function(f, l) {
            l = l || {};
            null != f && this.bind(f) && this.step();
            f = [];
            for (var n = tc(this.Qa), p = 0; p < n; p += 1) switch (xc(this.Qa, p)) {
              case 1:
                var r = l.useBigInt ? this.hc(p) : this.Pb(p);
                f.push(r);
                break;
              case 2:
                f.push(this.Pb(p));
                break;
              case 3:
                f.push(this.mc(p));
                break;
              case 4:
                f.push(this.getBlob(p));
                break;
              default:
                f.push(null);
            }
            return f;
          };
          c.prototype.Db = function() {
            for (var f = [], l = sc(this.Qa), n = 0; n < l; n += 1) f.push(yc(this.Qa, n));
            return f;
          };
          c.prototype.Ob = function(f, l) {
            f = this.get(f, l);
            l = this.Db();
            for (var n = {}, p = 0; p < l.length; p += 1) n[l[p]] = f[p];
            return n;
          };
          c.prototype.lc = function() {
            return ib(this.Qa);
          };
          c.prototype.ic = function() {
            return lc(this.Qa);
          };
          c.prototype.Jb = function(f) {
            null != f && this.bind(f);
            this.step();
            return this.reset();
          };
          c.prototype.Lb = function(f, l) {
            null == l && (l = this.Oa, this.Oa += 1);
            f = ea(f);
            this.yb.push(f);
            this.db.handleError(mc(this.Qa, l, f, -1, 0));
          };
          c.prototype.Vb = function(f, l) {
            null == l && (l = this.Oa, this.Oa += 1);
            var n = ca(f.length);
            m.set(f, n);
            this.yb.push(n);
            this.db.handleError(kb(this.Qa, l, n, f.length, 0));
          };
          c.prototype.Kb = function(f, l) {
            null == l && (l = this.Oa, this.Oa += 1);
            this.db.handleError((f === (f | 0) ? oc : nc)(
              this.Qa,
              l,
              f
            ));
          };
          c.prototype.Yb = function(f) {
            null == f && (f = this.Oa, this.Oa += 1);
            kb(this.Qa, f, 0, 0, 0);
          };
          c.prototype.Mb = function(f, l) {
            null == l && (l = this.Oa, this.Oa += 1);
            switch (typeof f) {
              case "string":
                this.Lb(f, l);
                return;
              case "number":
                this.Kb(f, l);
                return;
              case "bigint":
                this.Lb(f.toString(), l);
                return;
              case "boolean":
                this.Kb(f + 0, l);
                return;
              case "object":
                if (null === f) {
                  this.Yb(l);
                  return;
                }
                if (null != f.length) {
                  this.Vb(f, l);
                  return;
                }
            }
            throw "Wrong API use : tried to bind a value of an unknown type (" + f + ").";
          };
          c.prototype.Xb = function(f) {
            var l = this;
            Object.keys(f).forEach(function(n) {
              var p = pc(l.Qa, n);
              0 !== p && l.Mb(f[n], p);
            });
            return true;
          };
          c.prototype.Wb = function(f) {
            for (var l = 0; l < f.length; l += 1) this.Mb(f[l], l + 1);
            return true;
          };
          c.prototype.reset = function() {
            this.Cb();
            return 0 === Ac(this.Qa) && 0 === zc(this.Qa);
          };
          c.prototype.Cb = function() {
            for (var f; void 0 !== (f = this.yb.pop()); ) da(f);
          };
          c.prototype.cb = function() {
            this.Cb();
            var f = 0 === Bc(this.Qa);
            delete this.db.pb[this.Qa];
            this.Qa = 0;
            return f;
          };
          d.prototype.next = function() {
            if (null === this.ob) return { done: true };
            null !== this.gb && (this.gb.cb(), this.gb = null);
            if (!this.db.db) throw this.Ab(), Error("Database closed");
            var f = pa(), l = y(4);
            qa(g);
            qa(l);
            try {
              this.db.handleError(jb(this.db.db, this.ub, -1, g, l));
              this.ub = t(l, "i32");
              var n = t(g, "i32");
              if (0 === n) return this.Ab(), { done: true };
              this.gb = new c(n, this.db);
              this.db.pb[n] = this.gb;
              return { value: this.gb, done: false };
            } catch (p) {
              throw this.Fb = z(this.ub), this.Ab(), p;
            } finally {
              ra(f);
            }
          };
          d.prototype.Ab = function() {
            da(this.ob);
            this.ob = null;
          };
          d.prototype.jc = function() {
            return null !== this.Fb ? this.Fb : z(this.ub);
          };
          "function" === typeof Symbol && "symbol" === typeof Symbol.iterator && (d.prototype[Symbol.iterator] = function() {
            return this;
          });
          e.prototype.Jb = function(f, l) {
            if (!this.db) throw "Database closed";
            if (l) {
              f = this.Gb(f, l);
              try {
                f.step();
              } finally {
                f.cb();
              }
            } else this.handleError(u(this.db, f, 0, 0, g));
            return this;
          };
          e.prototype.exec = function(f, l, n) {
            if (!this.db) throw "Database closed";
            var p = pa(), r = null, v = null, J = null;
            try {
              J = v = ea(f);
              var I2 = y(4);
              for (f = []; 0 !== t(J, "i8"); ) {
                qa(g);
                qa(I2);
                this.handleError(jb(this.db, J, -1, g, I2));
                var L2 = t(g, "i32");
                J = t(I2, "i32");
                if (0 !== L2) {
                  var G2 = null;
                  r = new c(L2, this);
                  for (null != l && r.bind(l); r.step(); ) null === G2 && (G2 = { columns: r.Db(), values: [] }, f.push(G2)), G2.values.push(r.get(null, n));
                  r.cb();
                }
              }
              return f;
            } catch (la) {
              throw r && r.cb(), la;
            } finally {
              v && da(v), ra(p);
            }
          };
          e.prototype.ec = function(f, l, n, p, r) {
            "function" === typeof l && (p = n, n = l, l = void 0);
            f = this.Gb(f, l);
            try {
              for (; f.step(); ) n(f.Ob(null, r));
            } finally {
              f.cb();
            }
            if ("function" === typeof p) return p();
          };
          e.prototype.Gb = function(f, l) {
            qa(g);
            this.handleError(D(this.db, f, -1, g, 0));
            f = t(g, "i32");
            if (0 === f) throw "Nothing to prepare";
            var n = new c(f, this);
            null != l && n.bind(l);
            return this.pb[f] = n;
          };
          e.prototype.pc = function(f) {
            return new d(f, this);
          };
          e.prototype.fc = function() {
            Object.values(this.pb).forEach(function(l) {
              l.cb();
            });
            Object.values(this.Sa).forEach(A);
            this.Sa = {};
            this.handleError(w(this.db));
            var f = sa(this.filename);
            this.handleError(q(this.filename, g));
            this.db = t(g, "i32");
            hb(this.db);
            return f;
          };
          e.prototype.close = function() {
            null !== this.db && (Object.values(this.pb).forEach(function(f) {
              f.cb();
            }), Object.values(this.Sa).forEach(A), this.Sa = {}, this.fb && (A(this.fb), this.fb = void 0), this.handleError(w(this.db)), ta("/" + this.filename), this.db = null);
          };
          e.prototype.handleError = function(f) {
            if (0 === f) return null;
            f = rc(this.db);
            throw Error(f);
          };
          e.prototype.kc = function() {
            return x(this.db);
          };
          e.prototype.bc = function(f, l) {
            Object.prototype.hasOwnProperty.call(this.Sa, f) && (A(this.Sa[f]), delete this.Sa[f]);
            var n = va(function(p, r, v) {
              r = b(r, v);
              try {
                var J = l.apply(null, r);
              } catch (I2) {
                ua(p, I2, -1);
                return;
              }
              a(p, J);
            }, "viii");
            this.Sa[f] = n;
            this.handleError(mb(
              this.db,
              f,
              l.length,
              1,
              0,
              n,
              0,
              0,
              0
            ));
            return this;
          };
          e.prototype.ac = function(f, l) {
            var n = l.init || function() {
              return null;
            }, p = l.finalize || function(L2) {
              return L2;
            }, r = l.step;
            if (!r) throw "An aggregate function must have a step function in " + f;
            var v = {};
            Object.hasOwnProperty.call(this.Sa, f) && (A(this.Sa[f]), delete this.Sa[f]);
            l = f + "__finalize";
            Object.hasOwnProperty.call(this.Sa, l) && (A(this.Sa[l]), delete this.Sa[l]);
            var J = va(function(L2, G2, la) {
              var V = nb(L2, 1);
              Object.hasOwnProperty.call(v, V) || (v[V] = n());
              G2 = b(G2, la);
              G2 = [v[V]].concat(G2);
              try {
                v[V] = r.apply(null, G2);
              } catch (Dc) {
                delete v[V], ua(L2, Dc, -1);
              }
            }, "viii"), I2 = va(function(L2) {
              var G2 = nb(L2, 1);
              try {
                var la = p(v[G2]);
              } catch (V) {
                delete v[G2];
                ua(L2, V, -1);
                return;
              }
              a(L2, la);
              delete v[G2];
            }, "vi");
            this.Sa[f] = J;
            this.Sa[l] = I2;
            this.handleError(mb(this.db, f, r.length - 1, 1, 0, 0, J, I2, 0));
            return this;
          };
          e.prototype.vc = function(f) {
            this.fb && (ob(this.db, 0, 0), A(this.fb), this.fb = void 0);
            if (!f) return this;
            this.fb = va(function(l, n, p, r, v) {
              switch (n) {
                case 18:
                  l = "insert";
                  break;
                case 23:
                  l = "update";
                  break;
                case 9:
                  l = "delete";
                  break;
                default:
                  throw "unknown operationCode in updateHook callback: " + n;
              }
              p = z(p);
              r = z(r);
              if (v > Number.MAX_SAFE_INTEGER) throw "rowId too big to fit inside a Number";
              f(l, p, r, Number(v));
            }, "viiiij");
            ob(this.db, this.fb, 0);
            return this;
          };
          c.prototype.bind = c.prototype.bind;
          c.prototype.step = c.prototype.step;
          c.prototype.get = c.prototype.get;
          c.prototype.getColumnNames = c.prototype.Db;
          c.prototype.getAsObject = c.prototype.Ob;
          c.prototype.getSQL = c.prototype.lc;
          c.prototype.getNormalizedSQL = c.prototype.ic;
          c.prototype.run = c.prototype.Jb;
          c.prototype.reset = c.prototype.reset;
          c.prototype.freemem = c.prototype.Cb;
          c.prototype.free = c.prototype.cb;
          d.prototype.next = d.prototype.next;
          d.prototype.getRemainingSQL = d.prototype.jc;
          e.prototype.run = e.prototype.Jb;
          e.prototype.exec = e.prototype.exec;
          e.prototype.each = e.prototype.ec;
          e.prototype.prepare = e.prototype.Gb;
          e.prototype.iterateStatements = e.prototype.pc;
          e.prototype["export"] = e.prototype.fc;
          e.prototype.close = e.prototype.close;
          e.prototype.handleError = e.prototype.handleError;
          e.prototype.getRowsModified = e.prototype.kc;
          e.prototype.create_function = e.prototype.bc;
          e.prototype.create_aggregate = e.prototype.ac;
          e.prototype.updateHook = e.prototype.vc;
          k.Database = e;
        };
        var wa = "./this.program", xa = globalThis.document?.currentScript?.src;
        ba && (xa = self.location.href);
        var ya = "", za, Aa;
        if (aa || ba) {
          try {
            ya = new URL(".", xa).href;
          } catch {
          }
          ba && (Aa = (a) => {
            var b = new XMLHttpRequest();
            b.open("GET", a, false);
            b.responseType = "arraybuffer";
            b.send(null);
            return new Uint8Array(b.response);
          });
          za = async (a) => {
            a = await fetch(a, { credentials: "same-origin" });
            if (a.ok) return a.arrayBuffer();
            throw Error(a.status + " : " + a.url);
          };
        }
        var Ba = console.log.bind(console), B = console.error.bind(console), Ca, Da = false, Ea, m, C2, Fa, E, F, Ga, Ha, H;
        function Ia() {
          var a = Ja.buffer;
          m = new Int8Array(a);
          Fa = new Int16Array(a);
          C2 = new Uint8Array(a);
          new Uint16Array(a);
          E = new Int32Array(a);
          F = new Uint32Array(a);
          Ga = new Float32Array(a);
          Ha = new Float64Array(a);
          H = new BigInt64Array(a);
          new BigUint64Array(a);
        }
        function Ka(a) {
          k.onAbort?.(a);
          a = "Aborted(" + a + ")";
          B(a);
          Da = true;
          throw new WebAssembly.RuntimeError(a + ". Build with -sASSERTIONS for more info.");
        }
        var La;
        async function Ma(a) {
          if (!Ca) try {
            var b = await za(a);
            return new Uint8Array(b);
          } catch {
          }
          if (a == La && Ca) a = new Uint8Array(Ca);
          else if (Aa) a = Aa(a);
          else throw "both async and sync fetching of the wasm failed";
          return a;
        }
        async function Na(a, b) {
          try {
            var c = await Ma(a);
            return await WebAssembly.instantiate(c, b);
          } catch (d) {
            B(`failed to asynchronously prepare wasm: ${d}`), Ka(d);
          }
        }
        async function Oa(a) {
          var b = La;
          if (!Ca) try {
            var c = fetch(b, { credentials: "same-origin" });
            return await WebAssembly.instantiateStreaming(c, a);
          } catch (d) {
            B(`wasm streaming compile failed: ${d}`), B("falling back to ArrayBuffer instantiation");
          }
          return Na(b, a);
        }
        class Pa {
          name = "ExitStatus";
          constructor(a) {
            this.message = `Program terminated with exit(${a})`;
            this.status = a;
          }
        }
        var Qa = (a) => {
          for (; 0 < a.length; ) a.shift()(k);
        }, Ra = [], Sa = [], Ta = () => {
          var a = k.preRun.shift();
          Sa.push(a);
        }, K = 0, Ua = null;
        function t(a, b = "i8") {
          b.endsWith("*") && (b = "*");
          switch (b) {
            case "i1":
              return m[a];
            case "i8":
              return m[a];
            case "i16":
              return Fa[a >> 1];
            case "i32":
              return E[a >> 2];
            case "i64":
              return H[a >> 3];
            case "float":
              return Ga[a >> 2];
            case "double":
              return Ha[a >> 3];
            case "*":
              return F[a >> 2];
            default:
              Ka(`invalid type for getValue: ${b}`);
          }
        }
        var Va = true;
        function qa(a) {
          var b = "i32";
          b.endsWith("*") && (b = "*");
          switch (b) {
            case "i1":
              m[a] = 0;
              break;
            case "i8":
              m[a] = 0;
              break;
            case "i16":
              Fa[a >> 1] = 0;
              break;
            case "i32":
              E[a >> 2] = 0;
              break;
            case "i64":
              H[a >> 3] = BigInt(0);
              break;
            case "float":
              Ga[a >> 2] = 0;
              break;
            case "double":
              Ha[a >> 3] = 0;
              break;
            case "*":
              F[a >> 2] = 0;
              break;
            default:
              Ka(`invalid type for setValue: ${b}`);
          }
        }
        var Wa = new TextDecoder(), Xa = (a, b, c, d) => {
          c = b + c;
          if (d) return c;
          for (; a[b] && !(b >= c); ) ++b;
          return b;
        }, z = (a, b, c) => a ? Wa.decode(C2.subarray(a, Xa(C2, a, b, c))) : "", Ya = (a, b) => {
          for (var c = 0, d = a.length - 1; 0 <= d; d--) {
            var e = a[d];
            "." === e ? a.splice(d, 1) : ".." === e ? (a.splice(d, 1), c++) : c && (a.splice(d, 1), c--);
          }
          if (b) for (; c; c--) a.unshift("..");
          return a;
        }, ha = (a) => {
          var b = "/" === a.charAt(0), c = "/" === a.slice(-1);
          (a = Ya(a.split("/").filter((d) => !!d), !b).join("/")) || b || (a = ".");
          a && c && (a += "/");
          return (b ? "/" : "") + a;
        }, Za = (a) => {
          var b = /^(\/?|)([\s\S]*?)((?:\.{1,2}|[^\/]+?|)(\.[^.\/]*|))(?:[\/]*)$/.exec(a).slice(1);
          a = b[0];
          b = b[1];
          if (!a && !b) return ".";
          b &&= b.slice(0, -1);
          return a + b;
        }, $a = (a) => a && a.match(/([^\/]+|\/)\/*$/)[1], ab = () => (a) => crypto.getRandomValues(a), bb = (a) => {
          (bb = ab())(a);
        }, cb = (...a) => {
          for (var b = "", c = false, d = a.length - 1; -1 <= d && !c; d--) {
            c = 0 <= d ? a[d] : "/";
            if ("string" != typeof c) throw new TypeError("Arguments to path.resolve must be strings");
            if (!c) return "";
            b = c + "/" + b;
            c = "/" === c.charAt(0);
          }
          b = Ya(b.split("/").filter((e) => !!e), !c).join("/");
          return (c ? "/" : "") + b || ".";
        }, db = (a) => {
          var b = Xa(a, 0);
          return Wa.decode(a.buffer ? a.subarray(0, b) : new Uint8Array(a.slice(0, b)));
        }, fb = [], gb = (a) => {
          for (var b = 0, c = 0; c < a.length; ++c) {
            var d = a.charCodeAt(c);
            127 >= d ? b++ : 2047 >= d ? b += 2 : 55296 <= d && 57343 >= d ? (b += 4, ++c) : b += 3;
          }
          return b;
        }, M2 = (a, b, c, d) => {
          if (!(0 < d)) return 0;
          var e = c;
          d = c + d - 1;
          for (var g = 0; g < a.length; ++g) {
            var h2 = a.codePointAt(g);
            if (127 >= h2) {
              if (c >= d) break;
              b[c++] = h2;
            } else if (2047 >= h2) {
              if (c + 1 >= d) break;
              b[c++] = 192 | h2 >> 6;
              b[c++] = 128 | h2 & 63;
            } else if (65535 >= h2) {
              if (c + 2 >= d) break;
              b[c++] = 224 | h2 >> 12;
              b[c++] = 128 | h2 >> 6 & 63;
              b[c++] = 128 | h2 & 63;
            } else {
              if (c + 3 >= d) break;
              b[c++] = 240 | h2 >> 18;
              b[c++] = 128 | h2 >> 12 & 63;
              b[c++] = 128 | h2 >> 6 & 63;
              b[c++] = 128 | h2 & 63;
              g++;
            }
          }
          b[c] = 0;
          return c - e;
        }, pb = [];
        function qb(a, b) {
          pb[a] = { input: [], output: [], kb: b };
          rb(a, sb);
        }
        var sb = { open(a) {
          var b = pb[a.node.nb];
          if (!b) throw new N2(43);
          a.Va = b;
          a.seekable = false;
        }, close(a) {
          a.Va.kb.lb(a.Va);
        }, lb(a) {
          a.Va.kb.lb(a.Va);
        }, read(a, b, c, d) {
          if (!a.Va || !a.Va.kb.Qb) throw new N2(60);
          for (var e = 0, g = 0; g < d; g++) {
            try {
              var h2 = a.Va.kb.Qb(a.Va);
            } catch (q) {
              throw new N2(29);
            }
            if (void 0 === h2 && 0 === e) throw new N2(6);
            if (null === h2 || void 0 === h2) break;
            e++;
            b[c + g] = h2;
          }
          e && (a.node.$a = Date.now());
          return e;
        }, write(a, b, c, d) {
          if (!a.Va || !a.Va.kb.Hb) throw new N2(60);
          try {
            for (var e = 0; e < d; e++) a.Va.kb.Hb(a.Va, b[c + e]);
          } catch (g) {
            throw new N2(29);
          }
          d && (a.node.Ua = a.node.Ta = Date.now());
          return e;
        } }, tb = { Qb() {
          a: {
            if (!fb.length) {
              var a = null;
              globalThis.window?.prompt && (a = window.prompt("Input: "), null !== a && (a += "\n"));
              if (!a) {
                var b = null;
                break a;
              }
              b = Array(gb(a) + 1);
              a = M2(a, b, 0, b.length);
              b.length = a;
              fb = b;
            }
            b = fb.shift();
          }
          return b;
        }, Hb(a, b) {
          null === b || 10 === b ? (Ba(db(a.output)), a.output = []) : 0 != b && a.output.push(b);
        }, lb(a) {
          0 < a.output?.length && (Ba(db(a.output)), a.output = []);
        }, Dc() {
          return { yc: 25856, Ac: 5, xc: 191, zc: 35387, wc: [
            3,
            28,
            127,
            21,
            4,
            0,
            1,
            0,
            17,
            19,
            26,
            0,
            18,
            15,
            23,
            22,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0
          ] };
        }, Ec() {
          return 0;
        }, Fc() {
          return [24, 80];
        } }, ub = { Hb(a, b) {
          null === b || 10 === b ? (B(db(a.output)), a.output = []) : 0 != b && a.output.push(b);
        }, lb(a) {
          0 < a.output?.length && (B(db(a.output)), a.output = []);
        } }, O = { Za: null, ab() {
          return O.createNode(null, "/", 16895, 0);
        }, createNode(a, b, c, d) {
          if (24576 === (c & 61440) || 4096 === (c & 61440)) throw new N2(63);
          O.Za || (O.Za = { dir: { node: { Wa: O.La.Wa, Xa: O.La.Xa, mb: O.La.mb, rb: O.La.rb, Tb: O.La.Tb, xb: O.La.xb, vb: O.La.vb, Ib: O.La.Ib, wb: O.La.wb }, stream: { Ya: O.Ma.Ya } }, file: {
            node: { Wa: O.La.Wa, Xa: O.La.Xa },
            stream: { Ya: O.Ma.Ya, read: O.Ma.read, write: O.Ma.write, sb: O.Ma.sb, tb: O.Ma.tb }
          }, link: { node: { Wa: O.La.Wa, Xa: O.La.Xa, eb: O.La.eb }, stream: {} }, Nb: { node: { Wa: O.La.Wa, Xa: O.La.Xa }, stream: vb } });
          c = wb(a, b, c, d);
          P2(c.mode) ? (c.La = O.Za.dir.node, c.Ma = O.Za.dir.stream, c.Na = {}) : 32768 === (c.mode & 61440) ? (c.La = O.Za.file.node, c.Ma = O.Za.file.stream, c.Ra = 0, c.Na = null) : 40960 === (c.mode & 61440) ? (c.La = O.Za.link.node, c.Ma = O.Za.link.stream) : 8192 === (c.mode & 61440) && (c.La = O.Za.Nb.node, c.Ma = O.Za.Nb.stream);
          c.$a = c.Ua = c.Ta = Date.now();
          a && (a.Na[b] = c, a.$a = a.Ua = a.Ta = c.$a);
          return c;
        }, Cc(a) {
          return a.Na ? a.Na.subarray ? a.Na.subarray(0, a.Ra) : new Uint8Array(a.Na) : new Uint8Array(0);
        }, La: { Wa(a) {
          var b = {};
          b.cc = 8192 === (a.mode & 61440) ? a.id : 1;
          b.oc = a.id;
          b.mode = a.mode;
          b.rc = 1;
          b.uid = 0;
          b.nc = 0;
          b.nb = a.nb;
          P2(a.mode) ? b.size = 4096 : 32768 === (a.mode & 61440) ? b.size = a.Ra : 40960 === (a.mode & 61440) ? b.size = a.link.length : b.size = 0;
          b.$a = new Date(a.$a);
          b.Ua = new Date(a.Ua);
          b.Ta = new Date(a.Ta);
          b.Zb = 4096;
          b.$b = Math.ceil(b.size / b.Zb);
          return b;
        }, Xa(a, b) {
          for (var c of ["mode", "atime", "mtime", "ctime"]) null != b[c] && (a[c] = b[c]);
          void 0 !== b.size && (b = b.size, a.Ra != b && (0 == b ? (a.Na = null, a.Ra = 0) : (c = a.Na, a.Na = new Uint8Array(b), c && a.Na.set(c.subarray(0, Math.min(b, a.Ra))), a.Ra = b)));
        }, mb() {
          O.zb || (O.zb = new N2(44), O.zb.stack = "<generic error, no stack>");
          throw O.zb;
        }, rb(a, b, c, d) {
          return O.createNode(a, b, c, d);
        }, Tb(a, b, c) {
          try {
            var d = Q(b, c);
          } catch (g) {
          }
          if (d) {
            if (P2(a.mode)) for (var e in d.Na) throw new N2(55);
            xb(d);
          }
          delete a.parent.Na[a.name];
          b.Na[c] = a;
          a.name = c;
          b.Ta = b.Ua = a.parent.Ta = a.parent.Ua = Date.now();
        }, xb(a, b) {
          delete a.Na[b];
          a.Ta = a.Ua = Date.now();
        }, vb(a, b) {
          var c = Q(a, b), d;
          for (d in c.Na) throw new N2(55);
          delete a.Na[b];
          a.Ta = a.Ua = Date.now();
        }, Ib(a) {
          return [".", "..", ...Object.keys(a.Na)];
        }, wb(a, b, c) {
          a = O.createNode(a, b, 41471, 0);
          a.link = c;
          return a;
        }, eb(a) {
          if (40960 !== (a.mode & 61440)) throw new N2(28);
          return a.link;
        } }, Ma: { read(a, b, c, d, e) {
          var g = a.node.Na;
          if (e >= a.node.Ra) return 0;
          a = Math.min(a.node.Ra - e, d);
          if (8 < a && g.subarray) b.set(g.subarray(e, e + a), c);
          else for (d = 0; d < a; d++) b[c + d] = g[e + d];
          return a;
        }, write(a, b, c, d, e, g) {
          b.buffer === m.buffer && (g = false);
          if (!d) return 0;
          a = a.node;
          a.Ua = a.Ta = Date.now();
          if (b.subarray && (!a.Na || a.Na.subarray)) {
            if (g) return a.Na = b.subarray(c, c + d), a.Ra = d;
            if (0 === a.Ra && 0 === e) return a.Na = b.slice(c, c + d), a.Ra = d;
            if (e + d <= a.Ra) return a.Na.set(b.subarray(c, c + d), e), d;
          }
          g = e + d;
          var h2 = a.Na ? a.Na.length : 0;
          h2 >= g || (g = Math.max(g, h2 * (1048576 > h2 ? 2 : 1.125) >>> 0), 0 != h2 && (g = Math.max(g, 256)), h2 = a.Na, a.Na = new Uint8Array(g), 0 < a.Ra && a.Na.set(h2.subarray(0, a.Ra), 0));
          if (a.Na.subarray && b.subarray) a.Na.set(b.subarray(c, c + d), e);
          else for (g = 0; g < d; g++) a.Na[e + g] = b[c + g];
          a.Ra = Math.max(
            a.Ra,
            e + d
          );
          return d;
        }, Ya(a, b, c) {
          1 === c ? b += a.position : 2 === c && 32768 === (a.node.mode & 61440) && (b += a.node.Ra);
          if (0 > b) throw new N2(28);
          return b;
        }, sb(a, b, c, d, e) {
          if (32768 !== (a.node.mode & 61440)) throw new N2(43);
          a = a.node.Na;
          if (e & 2 || !a || a.buffer !== m.buffer) {
            e = true;
            d = 65536 * Math.ceil(b / 65536);
            var g = yb(65536, d);
            g && C2.fill(0, g, g + d);
            d = g;
            if (!d) throw new N2(48);
            if (a) {
              if (0 < c || c + b < a.length) a.subarray ? a = a.subarray(c, c + b) : a = Array.prototype.slice.call(a, c, c + b);
              m.set(a, d);
            }
          } else e = false, d = a.byteOffset;
          return { tc: d, Ub: e };
        }, tb(a, b, c, d) {
          O.Ma.write(
            a,
            b,
            0,
            d,
            c,
            false
          );
          return 0;
        } } }, ia = (a, b) => {
          var c = 0;
          a && (c |= 365);
          b && (c |= 146);
          return c;
        }, zb = null, Ab = {}, Bb = [], Cb = 1, R = null, Db = false, Eb = true, Fb = {}, N2 = class {
          name = "ErrnoError";
          constructor(a) {
            this.Pa = a;
          }
        }, Gb = class {
          qb = {};
          node = null;
          get flags() {
            return this.qb.flags;
          }
          set flags(a) {
            this.qb.flags = a;
          }
          get position() {
            return this.qb.position;
          }
          set position(a) {
            this.qb.position = a;
          }
        }, Hb = class {
          La = {};
          Ma = {};
          ib = null;
          constructor(a, b, c, d) {
            a ||= this;
            this.parent = a;
            this.ab = a.ab;
            this.id = Cb++;
            this.name = b;
            this.mode = c;
            this.nb = d;
            this.$a = this.Ua = this.Ta = Date.now();
          }
          get read() {
            return 365 === (this.mode & 365);
          }
          set read(a) {
            a ? this.mode |= 365 : this.mode &= -366;
          }
          get write() {
            return 146 === (this.mode & 146);
          }
          set write(a) {
            a ? this.mode |= 146 : this.mode &= -147;
          }
        };
        function S(a, b = {}) {
          if (!a) throw new N2(44);
          b.Bb ?? (b.Bb = true);
          "/" === a.charAt(0) || (a = "//" + a);
          var c = 0;
          a: for (; 40 > c; c++) {
            a = a.split("/").filter((q) => !!q);
            for (var d = zb, e = "/", g = 0; g < a.length; g++) {
              var h2 = g === a.length - 1;
              if (h2 && b.parent) break;
              if ("." !== a[g]) if (".." === a[g]) if (e = Za(e), d === d.parent) {
                a = e + "/" + a.slice(g + 1).join("/");
                c--;
                continue a;
              } else d = d.parent;
              else {
                e = ha(e + "/" + a[g]);
                try {
                  d = Q(d, a[g]);
                } catch (q) {
                  if (44 === q?.Pa && h2 && b.sc) return { path: e };
                  throw q;
                }
                !d.ib || h2 && !b.Bb || (d = d.ib.root);
                if (40960 === (d.mode & 61440) && (!h2 || b.hb)) {
                  if (!d.La.eb) throw new N2(52);
                  d = d.La.eb(d);
                  "/" === d.charAt(0) || (d = Za(e) + "/" + d);
                  a = d + "/" + a.slice(g + 1).join("/");
                  continue a;
                }
              }
            }
            return { path: e, node: d };
          }
          throw new N2(32);
        }
        function fa(a) {
          for (var b; ; ) {
            if (a === a.parent) return a = a.ab.Sb, b ? "/" !== a[a.length - 1] ? `${a}/${b}` : a + b : a;
            b = b ? `${a.name}/${b}` : a.name;
            a = a.parent;
          }
        }
        function Ib(a, b) {
          for (var c = 0, d = 0; d < b.length; d++) c = (c << 5) - c + b.charCodeAt(d) | 0;
          return (a + c >>> 0) % R.length;
        }
        function xb(a) {
          var b = Ib(a.parent.id, a.name);
          if (R[b] === a) R[b] = a.jb;
          else for (b = R[b]; b; ) {
            if (b.jb === a) {
              b.jb = a.jb;
              break;
            }
            b = b.jb;
          }
        }
        function Q(a, b) {
          var c = P2(a.mode) ? (c = Jb(a, "x")) ? c : a.La.mb ? 0 : 2 : 54;
          if (c) throw new N2(c);
          for (c = R[Ib(a.id, b)]; c; c = c.jb) {
            var d = c.name;
            if (c.parent.id === a.id && d === b) return c;
          }
          return a.La.mb(a, b);
        }
        function wb(a, b, c, d) {
          a = new Hb(a, b, c, d);
          b = Ib(a.parent.id, a.name);
          a.jb = R[b];
          return R[b] = a;
        }
        function P2(a) {
          return 16384 === (a & 61440);
        }
        function Kb(a) {
          var b = ["r", "w", "rw"][a & 3];
          a & 512 && (b += "w");
          return b;
        }
        function Jb(a, b) {
          if (Eb) return 0;
          if (!b.includes("r") || a.mode & 292) {
            if (b.includes("w") && !(a.mode & 146) || b.includes("x") && !(a.mode & 73)) return 2;
          } else return 2;
          return 0;
        }
        function Lb(a, b) {
          if (!P2(a.mode)) return 54;
          try {
            return Q(a, b), 20;
          } catch (c) {
          }
          return Jb(a, "wx");
        }
        function Mb(a, b, c) {
          try {
            var d = Q(a, b);
          } catch (e) {
            return e.Pa;
          }
          if (a = Jb(a, "wx")) return a;
          if (c) {
            if (!P2(d.mode)) return 54;
            if (d === d.parent || "/" === fa(d)) return 10;
          } else if (P2(d.mode)) return 31;
          return 0;
        }
        function Nb(a) {
          if (!a) throw new N2(63);
          return a;
        }
        function T(a) {
          a = Bb[a];
          if (!a) throw new N2(8);
          return a;
        }
        function Ob(a, b = -1) {
          a = Object.assign(new Gb(), a);
          if (-1 == b) a: {
            for (b = 0; 4096 >= b; b++) if (!Bb[b]) break a;
            throw new N2(33);
          }
          a.bb = b;
          return Bb[b] = a;
        }
        function Pb(a, b = -1) {
          a = Ob(a, b);
          a.Ma?.Bc?.(a);
          return a;
        }
        function Qb(a, b, c) {
          var d = a?.Ma.Xa;
          a = d ? a : b;
          d ??= b.La.Xa;
          Nb(d);
          d(a, c);
        }
        var vb = { open(a) {
          a.Ma = Ab[a.node.nb].Ma;
          a.Ma.open?.(a);
        }, Ya() {
          throw new N2(70);
        } };
        function rb(a, b) {
          Ab[a] = { Ma: b };
        }
        function Rb(a, b) {
          var c = "/" === b;
          if (c && zb) throw new N2(10);
          if (!c && b) {
            var d = S(b, { Bb: false });
            b = d.path;
            d = d.node;
            if (d.ib) throw new N2(10);
            if (!P2(d.mode)) throw new N2(54);
          }
          b = { type: a, Gc: {}, Sb: b, qc: [] };
          a = a.ab(b);
          a.ab = b;
          b.root = a;
          c ? zb = a : d && (d.ib = b, d.ab && d.ab.qc.push(b));
        }
        function Sb(a, b, c) {
          var d = S(a, { parent: true }).node;
          a = $a(a);
          if (!a) throw new N2(28);
          if ("." === a || ".." === a) throw new N2(20);
          var e = Lb(d, a);
          if (e) throw new N2(e);
          if (!d.La.rb) throw new N2(63);
          return d.La.rb(d, a, b, c);
        }
        function ja(a, b = 438) {
          return Sb(a, b & 4095 | 32768, 0);
        }
        function U(a, b = 511) {
          return Sb(a, b & 1023 | 16384, 0);
        }
        function Tb(a, b, c) {
          "undefined" == typeof c && (c = b, b = 438);
          Sb(a, b | 8192, c);
        }
        function Ub(a, b) {
          if (!cb(a)) throw new N2(44);
          var c = S(b, { parent: true }).node;
          if (!c) throw new N2(44);
          b = $a(b);
          var d = Lb(c, b);
          if (d) throw new N2(d);
          if (!c.La.wb) throw new N2(63);
          c.La.wb(c, b, a);
        }
        function Vb(a) {
          var b = S(a, { parent: true }).node;
          a = $a(a);
          var c = Q(b, a), d = Mb(b, a, true);
          if (d) throw new N2(d);
          if (!b.La.vb) throw new N2(63);
          if (c.ib) throw new N2(10);
          b.La.vb(b, a);
          xb(c);
        }
        function ta(a) {
          var b = S(a, { parent: true }).node;
          if (!b) throw new N2(44);
          a = $a(a);
          var c = Q(b, a), d = Mb(b, a, false);
          if (d) throw new N2(d);
          if (!b.La.xb) throw new N2(63);
          if (c.ib) throw new N2(10);
          b.La.xb(b, a);
          xb(c);
        }
        function Wb(a, b) {
          a = S(a, { hb: !b }).node;
          return Nb(a.La.Wa)(a);
        }
        function Xb(a, b, c, d) {
          Qb(a, b, { mode: c & 4095 | b.mode & -4096, Ta: Date.now(), dc: d });
        }
        function ka(a, b) {
          a = "string" == typeof a ? S(a, { hb: true }).node : a;
          Xb(null, a, b);
        }
        function Yb(a, b, c) {
          if (P2(b.mode)) throw new N2(31);
          if (32768 !== (b.mode & 61440)) throw new N2(28);
          var d = Jb(b, "w");
          if (d) throw new N2(d);
          Qb(a, b, { size: c, timestamp: Date.now() });
        }
        function ma(a, b, c = 438) {
          if ("" === a) throw new N2(44);
          if ("string" == typeof b) {
            var d = { r: 0, "r+": 2, w: 577, "w+": 578, a: 1089, "a+": 1090 }[b];
            if ("undefined" == typeof d) throw Error(`Unknown file open mode: ${b}`);
            b = d;
          }
          c = b & 64 ? c & 4095 | 32768 : 0;
          if ("object" == typeof a) d = a;
          else {
            var e = a.endsWith("/");
            a = S(a, { hb: !(b & 131072), sc: true });
            d = a.node;
            a = a.path;
          }
          var g = false;
          if (b & 64) if (d) {
            if (b & 128) throw new N2(20);
          } else {
            if (e) throw new N2(31);
            d = Sb(a, c | 511, 0);
            g = true;
          }
          if (!d) throw new N2(44);
          8192 === (d.mode & 61440) && (b &= -513);
          if (b & 65536 && !P2(d.mode)) throw new N2(54);
          if (!g && (e = d ? 40960 === (d.mode & 61440) ? 32 : P2(d.mode) && ("r" !== Kb(b) || b & 576) ? 31 : Jb(d, Kb(b)) : 44)) throw new N2(e);
          b & 512 && !g && (e = d, e = "string" == typeof e ? S(e, { hb: true }).node : e, Yb(null, e, 0));
          b &= -131713;
          e = Ob({ node: d, path: fa(d), flags: b, seekable: true, position: 0, Ma: d.Ma, uc: [], error: false });
          e.Ma.open && e.Ma.open(e);
          g && ka(d, c & 511);
          !k.logReadFiles || b & 1 || a in Fb || (Fb[a] = 1);
          return e;
        }
        function oa(a) {
          if (null === a.bb) throw new N2(8);
          a.Eb && (a.Eb = null);
          try {
            a.Ma.close && a.Ma.close(a);
          } catch (b) {
            throw b;
          } finally {
            Bb[a.bb] = null;
          }
          a.bb = null;
        }
        function Zb(a, b, c) {
          if (null === a.bb) throw new N2(8);
          if (!a.seekable || !a.Ma.Ya) throw new N2(70);
          if (0 != c && 1 != c && 2 != c) throw new N2(28);
          a.position = a.Ma.Ya(a, b, c);
          a.uc = [];
        }
        function $b(a, b, c, d, e) {
          if (0 > d || 0 > e) throw new N2(28);
          if (null === a.bb) throw new N2(8);
          if (1 === (a.flags & 2097155)) throw new N2(8);
          if (P2(a.node.mode)) throw new N2(31);
          if (!a.Ma.read) throw new N2(28);
          var g = "undefined" != typeof e;
          if (!g) e = a.position;
          else if (!a.seekable) throw new N2(70);
          b = a.Ma.read(a, b, c, d, e);
          g || (a.position += b);
          return b;
        }
        function na(a, b, c, d, e) {
          if (0 > d || 0 > e) throw new N2(28);
          if (null === a.bb) throw new N2(8);
          if (0 === (a.flags & 2097155)) throw new N2(8);
          if (P2(a.node.mode)) throw new N2(31);
          if (!a.Ma.write) throw new N2(28);
          a.seekable && a.flags & 1024 && Zb(a, 0, 2);
          var g = "undefined" != typeof e;
          if (!g) e = a.position;
          else if (!a.seekable) throw new N2(70);
          b = a.Ma.write(a, b, c, d, e, void 0);
          g || (a.position += b);
          return b;
        }
        function sa(a) {
          var b = b || 0;
          var c = "binary";
          "utf8" !== c && "binary" !== c && Ka(`Invalid encoding type "${c}"`);
          b = ma(a, b);
          a = Wb(a).size;
          var d = new Uint8Array(a);
          $b(b, d, 0, a, 0);
          "utf8" === c && (d = db(d));
          oa(b);
          return d;
        }
        function W2(a, b, c) {
          a = ha("/dev/" + a);
          var d = ia(!!b, !!c);
          W2.Rb ?? (W2.Rb = 64);
          var e = W2.Rb++ << 8 | 0;
          rb(e, { open(g) {
            g.seekable = false;
          }, close() {
            c?.buffer?.length && c(10);
          }, read(g, h2, q, w) {
            for (var u = 0, x = 0; x < w; x++) {
              try {
                var D = b();
              } catch (ib) {
                throw new N2(29);
              }
              if (void 0 === D && 0 === u) throw new N2(6);
              if (null === D || void 0 === D) break;
              u++;
              h2[q + x] = D;
            }
            u && (g.node.$a = Date.now());
            return u;
          }, write(g, h2, q, w) {
            for (var u = 0; u < w; u++) try {
              c(h2[q + u]);
            } catch (x) {
              throw new N2(29);
            }
            w && (g.node.Ua = g.node.Ta = Date.now());
            return u;
          } });
          Tb(a, d, e);
        }
        var X = {};
        function Y(a, b, c) {
          if ("/" === b.charAt(0)) return b;
          a = -100 === a ? "/" : T(a).path;
          if (0 == b.length) {
            if (!c) throw new N2(44);
            return a;
          }
          return a + "/" + b;
        }
        function ac(a, b) {
          F[a >> 2] = b.cc;
          F[a + 4 >> 2] = b.mode;
          F[a + 8 >> 2] = b.rc;
          F[a + 12 >> 2] = b.uid;
          F[a + 16 >> 2] = b.nc;
          F[a + 20 >> 2] = b.nb;
          H[a + 24 >> 3] = BigInt(b.size);
          E[a + 32 >> 2] = 4096;
          E[a + 36 >> 2] = b.$b;
          var c = b.$a.getTime(), d = b.Ua.getTime(), e = b.Ta.getTime();
          H[a + 40 >> 3] = BigInt(Math.floor(c / 1e3));
          F[a + 48 >> 2] = c % 1e3 * 1e6;
          H[a + 56 >> 3] = BigInt(Math.floor(d / 1e3));
          F[a + 64 >> 2] = d % 1e3 * 1e6;
          H[a + 72 >> 3] = BigInt(Math.floor(e / 1e3));
          F[a + 80 >> 2] = e % 1e3 * 1e6;
          H[a + 88 >> 3] = BigInt(b.oc);
          return 0;
        }
        var kc = void 0, Cc = () => {
          var a = E[+kc >> 2];
          kc += 4;
          return a;
        }, Ec = 0, Fc = [0, 31, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335], Gc = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334], Hc = {}, Ic = (a) => {
          if (!(a instanceof Pa || "unwind" == a)) throw a;
        }, Jc = (a) => {
          Ea = a;
          Va || 0 < Ec || (k.onExit?.(a), Da = true);
          throw new Pa(a);
        }, Kc = (a) => {
          if (!Da) try {
            a();
          } catch (b) {
            Ic(b);
          } finally {
            if (!(Va || 0 < Ec)) try {
              Ea = a = Ea, Jc(a);
            } catch (b) {
              Ic(b);
            }
          }
        }, Lc = {}, Nc = () => {
          if (!Mc) {
            var a = { USER: "web_user", LOGNAME: "web_user", PATH: "/", PWD: "/", HOME: "/home/web_user", LANG: (globalThis.navigator?.language ?? "C").replace("-", "_") + ".UTF-8", _: wa || "./this.program" }, b;
            for (b in Lc) void 0 === Lc[b] ? delete a[b] : a[b] = Lc[b];
            var c = [];
            for (b in a) c.push(`${b}=${a[b]}`);
            Mc = c;
          }
          return Mc;
        }, Mc, Oc = (a, b, c, d) => {
          var e = { string: (u) => {
            var x = 0;
            if (null !== u && void 0 !== u && 0 !== u) {
              x = gb(u) + 1;
              var D = y(x);
              M2(u, C2, D, x);
              x = D;
            }
            return x;
          }, array: (u) => {
            var x = y(u.length);
            m.set(u, x);
            return x;
          } };
          a = k["_" + a];
          var g = [], h2 = 0;
          if (d) for (var q = 0; q < d.length; q++) {
            var w = e[c[q]];
            w ? (0 === h2 && (h2 = pa()), g[q] = w(d[q])) : g[q] = d[q];
          }
          c = a(...g);
          return c = (function(u) {
            0 !== h2 && ra(h2);
            return "string" === b ? z(u) : "boolean" === b ? !!u : u;
          })(c);
        }, ea = (a) => {
          var b = gb(a) + 1, c = ca(b);
          c && M2(a, C2, c, b);
          return c;
        }, Pc, Qc = [], A = (a) => {
          Pc.delete(Z.get(a));
          Z.set(a, null);
          Qc.push(a);
        }, Rc = (a) => {
          const b = a.length;
          return [b % 128 | 128, b >> 7, ...a];
        }, Sc = { i: 127, p: 127, j: 126, f: 125, d: 124, e: 111 }, Tc = (a) => Rc(Array.from(a, (b) => Sc[b])), va = (a, b) => {
          if (!Pc) {
            Pc = /* @__PURE__ */ new WeakMap();
            var c = Z.length;
            if (Pc) for (var d = 0; d < 0 + c; d++) {
              var e = Z.get(d);
              e && Pc.set(e, d);
            }
          }
          if (c = Pc.get(a) || 0) return c;
          c = Qc.length ? Qc.pop() : Z.grow(1);
          try {
            Z.set(c, a);
          } catch (g) {
            if (!(g instanceof TypeError)) throw g;
            b = Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0, 1, ...Rc([1, 96, ...Tc(b.slice(1)), ...Tc("v" === b[0] ? "" : b[0])]), 2, 7, 1, 1, 101, 1, 102, 0, 0, 7, 5, 1, 1, 102, 0, 0);
            b = new WebAssembly.Module(b);
            b = new WebAssembly.Instance(b, { e: { f: a } }).exports.f;
            Z.set(c, b);
          }
          Pc.set(a, c);
          return c;
        };
        R = Array(4096);
        Rb(O, "/");
        U("/tmp");
        U("/home");
        U("/home/web_user");
        (function() {
          U("/dev");
          rb(259, { read: () => 0, write: (d, e, g, h2) => h2, Ya: () => 0 });
          Tb("/dev/null", 259);
          qb(1280, tb);
          qb(1536, ub);
          Tb("/dev/tty", 1280);
          Tb("/dev/tty1", 1536);
          var a = new Uint8Array(1024), b = 0, c = () => {
            0 === b && (bb(a), b = a.byteLength);
            return a[--b];
          };
          W2("random", c);
          W2("urandom", c);
          U("/dev/shm");
          U("/dev/shm/tmp");
        })();
        (function() {
          U("/proc");
          var a = U("/proc/self");
          U("/proc/self/fd");
          Rb({ ab() {
            var b = wb(a, "fd", 16895, 73);
            b.Ma = { Ya: O.Ma.Ya };
            b.La = { mb(c, d) {
              c = +d;
              var e = T(c);
              c = { parent: null, ab: { Sb: "fake" }, La: { eb: () => e.path }, id: c + 1 };
              return c.parent = c;
            }, Ib() {
              return Array.from(Bb.entries()).filter(([, c]) => c).map(([c]) => c.toString());
            } };
            return b;
          } }, "/proc/self/fd");
        })();
        k.noExitRuntime && (Va = k.noExitRuntime);
        k.print && (Ba = k.print);
        k.printErr && (B = k.printErr);
        k.wasmBinary && (Ca = k.wasmBinary);
        k.thisProgram && (wa = k.thisProgram);
        if (k.preInit) for ("function" == typeof k.preInit && (k.preInit = [k.preInit]); 0 < k.preInit.length; ) k.preInit.shift()();
        k.stackSave = () => pa();
        k.stackRestore = (a) => ra(a);
        k.stackAlloc = (a) => y(a);
        k.cwrap = (a, b, c, d) => {
          var e = !c || c.every((g) => "number" === g || "boolean" === g);
          return "string" !== b && e && !d ? k["_" + a] : (...g) => Oc(a, b, c, g);
        };
        k.addFunction = va;
        k.removeFunction = A;
        k.UTF8ToString = z;
        k.stringToNewUTF8 = ea;
        k.writeArrayToMemory = (a, b) => {
          m.set(a, b);
        };
        var ca, da, yb, Uc, ra, y, pa, Ja, Z, Vc = {
          a: (a, b, c, d) => Ka(`Assertion failed: ${z(a)}, at: ` + [b ? z(b) : "unknown filename", c, d ? z(d) : "unknown function"]),
          i: function(a, b) {
            try {
              return a = z(a), ka(a, b), 0;
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return -c.Pa;
            }
          },
          L: function(a, b, c) {
            try {
              b = z(b);
              b = Y(a, b);
              if (c & -8) return -28;
              var d = S(b, { hb: true }).node;
              if (!d) return -44;
              a = "";
              c & 4 && (a += "r");
              c & 2 && (a += "w");
              c & 1 && (a += "x");
              return a && Jb(d, a) ? -2 : 0;
            } catch (e) {
              if ("undefined" == typeof X || "ErrnoError" !== e.name) throw e;
              return -e.Pa;
            }
          },
          j: function(a, b) {
            try {
              var c = T(a);
              Xb(c, c.node, b, false);
              return 0;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return -d.Pa;
            }
          },
          h: function(a) {
            try {
              var b = T(a);
              Qb(b, b.node, { timestamp: Date.now(), dc: false });
              return 0;
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return -c.Pa;
            }
          },
          b: function(a, b, c) {
            kc = c;
            try {
              var d = T(a);
              switch (b) {
                case 0:
                  var e = Cc();
                  if (0 > e) break;
                  for (; Bb[e]; ) e++;
                  return Pb(d, e).bb;
                case 1:
                case 2:
                  return 0;
                case 3:
                  return d.flags;
                case 4:
                  return e = Cc(), d.flags |= e, 0;
                case 12:
                  return e = Cc(), Fa[e + 0 >> 1] = 2, 0;
                case 13:
                case 14:
                  return 0;
              }
              return -28;
            } catch (g) {
              if ("undefined" == typeof X || "ErrnoError" !== g.name) throw g;
              return -g.Pa;
            }
          },
          g: function(a, b) {
            try {
              var c = T(a), d = c.node, e = c.Ma.Wa;
              a = e ? c : d;
              e ??= d.La.Wa;
              Nb(e);
              var g = e(a);
              return ac(b, g);
            } catch (h2) {
              if ("undefined" == typeof X || "ErrnoError" !== h2.name) throw h2;
              return -h2.Pa;
            }
          },
          H: function(a, b) {
            b = -9007199254740992 > b || 9007199254740992 < b ? NaN : Number(b);
            try {
              if (isNaN(b)) return -61;
              var c = T(a);
              if (0 > b || 0 === (c.flags & 2097155)) throw new N2(28);
              Yb(c, c.node, b);
              return 0;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return -d.Pa;
            }
          },
          G: function(a, b) {
            try {
              if (0 === b) return -28;
              var c = gb("/") + 1;
              if (b < c) return -68;
              M2("/", C2, a, b);
              return c;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return -d.Pa;
            }
          },
          K: function(a, b) {
            try {
              return a = z(a), ac(b, Wb(a, true));
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return -c.Pa;
            }
          },
          C: function(a, b, c) {
            try {
              return b = z(b), b = Y(a, b), U(b, c), 0;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return -d.Pa;
            }
          },
          J: function(a, b, c, d) {
            try {
              b = z(b);
              var e = d & 256;
              b = Y(a, b, d & 4096);
              return ac(c, e ? Wb(b, true) : Wb(b));
            } catch (g) {
              if ("undefined" == typeof X || "ErrnoError" !== g.name) throw g;
              return -g.Pa;
            }
          },
          x: function(a, b, c, d) {
            kc = d;
            try {
              b = z(b);
              b = Y(a, b);
              var e = d ? Cc() : 0;
              return ma(b, c, e).bb;
            } catch (g) {
              if ("undefined" == typeof X || "ErrnoError" !== g.name) throw g;
              return -g.Pa;
            }
          },
          v: function(a, b, c, d) {
            try {
              b = z(b);
              b = Y(a, b);
              if (0 >= d) return -28;
              var e = S(b).node;
              if (!e) throw new N2(44);
              if (!e.La.eb) throw new N2(28);
              var g = e.La.eb(e);
              var h2 = Math.min(d, gb(g)), q = m[c + h2];
              M2(g, C2, c, d + 1);
              m[c + h2] = q;
              return h2;
            } catch (w) {
              if ("undefined" == typeof X || "ErrnoError" !== w.name) throw w;
              return -w.Pa;
            }
          },
          u: function(a) {
            try {
              return a = z(a), Vb(a), 0;
            } catch (b) {
              if ("undefined" == typeof X || "ErrnoError" !== b.name) throw b;
              return -b.Pa;
            }
          },
          f: function(a, b) {
            try {
              return a = z(a), ac(b, Wb(a));
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return -c.Pa;
            }
          },
          r: function(a, b, c) {
            try {
              b = z(b);
              b = Y(a, b);
              if (c) if (512 === c) Vb(b);
              else return -28;
              else ta(b);
              return 0;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return -d.Pa;
            }
          },
          q: function(a, b, c) {
            try {
              b = z(b);
              b = Y(a, b, true);
              var d = Date.now(), e, g;
              if (c) {
                var h2 = F[c >> 2] + 4294967296 * E[c + 4 >> 2], q = E[c + 8 >> 2];
                1073741823 == q ? e = d : 1073741822 == q ? e = null : e = 1e3 * h2 + q / 1e6;
                c += 16;
                h2 = F[c >> 2] + 4294967296 * E[c + 4 >> 2];
                q = E[c + 8 >> 2];
                1073741823 == q ? g = d : 1073741822 == q ? g = null : g = 1e3 * h2 + q / 1e6;
              } else g = e = d;
              if (null !== (g ?? e)) {
                a = e;
                var w = S(b, { hb: true }).node;
                Nb(w.La.Xa)(w, { $a: a, Ua: g });
              }
              return 0;
            } catch (u) {
              if ("undefined" == typeof X || "ErrnoError" !== u.name) throw u;
              return -u.Pa;
            }
          },
          m: () => Ka(""),
          l: () => {
            Va = false;
            Ec = 0;
          },
          A: function(a, b) {
            a = -9007199254740992 > a || 9007199254740992 < a ? NaN : Number(a);
            a = new Date(1e3 * a);
            E[b >> 2] = a.getSeconds();
            E[b + 4 >> 2] = a.getMinutes();
            E[b + 8 >> 2] = a.getHours();
            E[b + 12 >> 2] = a.getDate();
            E[b + 16 >> 2] = a.getMonth();
            E[b + 20 >> 2] = a.getFullYear() - 1900;
            E[b + 24 >> 2] = a.getDay();
            var c = a.getFullYear();
            E[b + 28 >> 2] = (0 !== c % 4 || 0 === c % 100 && 0 !== c % 400 ? Gc : Fc)[a.getMonth()] + a.getDate() - 1 | 0;
            E[b + 36 >> 2] = -(60 * a.getTimezoneOffset());
            c = new Date(a.getFullYear(), 6, 1).getTimezoneOffset();
            var d = new Date(a.getFullYear(), 0, 1).getTimezoneOffset();
            E[b + 32 >> 2] = (c != d && a.getTimezoneOffset() == Math.min(d, c)) | 0;
          },
          y: function(a, b, c, d, e, g, h2) {
            e = -9007199254740992 > e || 9007199254740992 < e ? NaN : Number(e);
            try {
              var q = T(d);
              if (0 !== (b & 2) && 0 === (c & 2) && 2 !== (q.flags & 2097155)) throw new N2(2);
              if (1 === (q.flags & 2097155)) throw new N2(2);
              if (!q.Ma.sb) throw new N2(43);
              if (!a) throw new N2(28);
              var w = q.Ma.sb(q, a, e, b, c);
              var u = w.tc;
              E[g >> 2] = w.Ub;
              F[h2 >> 2] = u;
              return 0;
            } catch (x) {
              if ("undefined" == typeof X || "ErrnoError" !== x.name) throw x;
              return -x.Pa;
            }
          },
          z: function(a, b, c, d, e, g) {
            g = -9007199254740992 > g || 9007199254740992 < g ? NaN : Number(g);
            try {
              var h2 = T(e);
              if (c & 2) {
                if (32768 !== (h2.node.mode & 61440)) throw new N2(43);
                d & 2 || h2.Ma.tb && h2.Ma.tb(h2, C2.slice(a, a + b), g, b, d);
              }
            } catch (q) {
              if ("undefined" == typeof X || "ErrnoError" !== q.name) throw q;
              return -q.Pa;
            }
          },
          n: (a, b) => {
            Hc[a] && (clearTimeout(Hc[a].id), delete Hc[a]);
            if (!b) return 0;
            var c = setTimeout(() => {
              delete Hc[a];
              Kc(() => Uc(a, performance.now()));
            }, b);
            Hc[a] = { id: c, Hc: b };
            return 0;
          },
          B: (a, b, c, d) => {
            var e = (/* @__PURE__ */ new Date()).getFullYear(), g = new Date(e, 0, 1).getTimezoneOffset();
            e = new Date(e, 6, 1).getTimezoneOffset();
            F[a >> 2] = 60 * Math.max(g, e);
            E[b >> 2] = Number(g != e);
            b = (h2) => {
              var q = Math.abs(h2);
              return `UTC${0 <= h2 ? "-" : "+"}${String(Math.floor(q / 60)).padStart(2, "0")}${String(q % 60).padStart(2, "0")}`;
            };
            a = b(g);
            b = b(e);
            e < g ? (M2(a, C2, c, 17), M2(b, C2, d, 17)) : (M2(a, C2, d, 17), M2(b, C2, c, 17));
          },
          d: () => Date.now(),
          s: () => 2147483648,
          c: () => performance.now(),
          o: (a) => {
            var b = C2.length;
            a >>>= 0;
            if (2147483648 < a) return false;
            for (var c = 1; 4 >= c; c *= 2) {
              var d = b * (1 + 0.2 / c);
              d = Math.min(d, a + 100663296);
              a: {
                d = (Math.min(2147483648, 65536 * Math.ceil(Math.max(a, d) / 65536)) - Ja.buffer.byteLength + 65535) / 65536 | 0;
                try {
                  Ja.grow(d);
                  Ia();
                  var e = 1;
                  break a;
                } catch (g) {
                }
                e = void 0;
              }
              if (e) return true;
            }
            return false;
          },
          E: (a, b) => {
            var c = 0, d = 0, e;
            for (e of Nc()) {
              var g = b + c;
              F[a + d >> 2] = g;
              c += M2(e, C2, g, Infinity) + 1;
              d += 4;
            }
            return 0;
          },
          F: (a, b) => {
            var c = Nc();
            F[a >> 2] = c.length;
            a = 0;
            for (var d of c) a += gb(d) + 1;
            F[b >> 2] = a;
            return 0;
          },
          e: function(a) {
            try {
              var b = T(a);
              oa(b);
              return 0;
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return c.Pa;
            }
          },
          p: function(a, b) {
            try {
              var c = T(a);
              m[b] = c.Va ? 2 : P2(c.mode) ? 3 : 40960 === (c.mode & 61440) ? 7 : 4;
              Fa[b + 2 >> 1] = 0;
              H[b + 8 >> 3] = BigInt(0);
              H[b + 16 >> 3] = BigInt(0);
              return 0;
            } catch (d) {
              if ("undefined" == typeof X || "ErrnoError" !== d.name) throw d;
              return d.Pa;
            }
          },
          w: function(a, b, c, d) {
            try {
              a: {
                var e = T(a);
                a = b;
                for (var g, h2 = b = 0; h2 < c; h2++) {
                  var q = F[a >> 2], w = F[a + 4 >> 2];
                  a += 8;
                  var u = $b(e, m, q, w, g);
                  if (0 > u) {
                    var x = -1;
                    break a;
                  }
                  b += u;
                  if (u < w) break;
                  "undefined" != typeof g && (g += u);
                }
                x = b;
              }
              F[d >> 2] = x;
              return 0;
            } catch (D) {
              if ("undefined" == typeof X || "ErrnoError" !== D.name) throw D;
              return D.Pa;
            }
          },
          D: function(a, b, c, d) {
            b = -9007199254740992 > b || 9007199254740992 < b ? NaN : Number(b);
            try {
              if (isNaN(b)) return 61;
              var e = T(a);
              Zb(e, b, c);
              H[d >> 3] = BigInt(e.position);
              e.Eb && 0 === b && 0 === c && (e.Eb = null);
              return 0;
            } catch (g) {
              if ("undefined" == typeof X || "ErrnoError" !== g.name) throw g;
              return g.Pa;
            }
          },
          I: function(a) {
            try {
              var b = T(a);
              return b.Ma?.lb?.(b);
            } catch (c) {
              if ("undefined" == typeof X || "ErrnoError" !== c.name) throw c;
              return c.Pa;
            }
          },
          t: function(a, b, c, d) {
            try {
              a: {
                var e = T(a);
                a = b;
                for (var g, h2 = b = 0; h2 < c; h2++) {
                  var q = F[a >> 2], w = F[a + 4 >> 2];
                  a += 8;
                  var u = na(e, m, q, w, g);
                  if (0 > u) {
                    var x = -1;
                    break a;
                  }
                  b += u;
                  if (u < w) break;
                  "undefined" != typeof g && (g += u);
                }
                x = b;
              }
              F[d >> 2] = x;
              return 0;
            } catch (D) {
              if ("undefined" == typeof X || "ErrnoError" !== D.name) throw D;
              return D.Pa;
            }
          },
          k: Jc
        };
        function Wc() {
          function a() {
            k.calledRun = true;
            if (!Da) {
              if (!k.noFSInit && !Db) {
                var b, c;
                Db = true;
                b ??= k.stdin;
                c ??= k.stdout;
                d ??= k.stderr;
                b ? W2("stdin", b) : Ub("/dev/tty", "/dev/stdin");
                c ? W2("stdout", null, c) : Ub("/dev/tty", "/dev/stdout");
                d ? W2("stderr", null, d) : Ub("/dev/tty1", "/dev/stderr");
                ma("/dev/stdin", 0);
                ma("/dev/stdout", 1);
                ma("/dev/stderr", 1);
              }
              Xc.N();
              Eb = false;
              k.onRuntimeInitialized?.();
              if (k.postRun) for ("function" == typeof k.postRun && (k.postRun = [k.postRun]); k.postRun.length; ) {
                var d = k.postRun.shift();
                Ra.push(d);
              }
              Qa(Ra);
            }
          }
          if (0 < K) Ua = Wc;
          else {
            if (k.preRun) for ("function" == typeof k.preRun && (k.preRun = [k.preRun]); k.preRun.length; ) Ta();
            Qa(Sa);
            0 < K ? Ua = Wc : k.setStatus ? (k.setStatus("Running..."), setTimeout(() => {
              setTimeout(() => k.setStatus(""), 1);
              a();
            }, 1)) : a();
          }
        }
        var Xc;
        (async function() {
          function a(c) {
            c = Xc = c.exports;
            k._sqlite3_free = c.P;
            k._sqlite3_value_text = c.Q;
            k._sqlite3_prepare_v2 = c.R;
            k._sqlite3_step = c.S;
            k._sqlite3_reset = c.T;
            k._sqlite3_exec = c.U;
            k._sqlite3_finalize = c.V;
            k._sqlite3_column_name = c.W;
            k._sqlite3_column_text = c.X;
            k._sqlite3_column_type = c.Y;
            k._sqlite3_errmsg = c.Z;
            k._sqlite3_clear_bindings = c._;
            k._sqlite3_value_blob = c.$;
            k._sqlite3_value_bytes = c.aa;
            k._sqlite3_value_double = c.ba;
            k._sqlite3_value_int = c.ca;
            k._sqlite3_value_type = c.da;
            k._sqlite3_result_blob = c.ea;
            k._sqlite3_result_double = c.fa;
            k._sqlite3_result_error = c.ga;
            k._sqlite3_result_int = c.ha;
            k._sqlite3_result_int64 = c.ia;
            k._sqlite3_result_null = c.ja;
            k._sqlite3_result_text = c.ka;
            k._sqlite3_aggregate_context = c.la;
            k._sqlite3_column_count = c.ma;
            k._sqlite3_data_count = c.na;
            k._sqlite3_column_blob = c.oa;
            k._sqlite3_column_bytes = c.pa;
            k._sqlite3_column_double = c.qa;
            k._sqlite3_bind_blob = c.ra;
            k._sqlite3_bind_double = c.sa;
            k._sqlite3_bind_int = c.ta;
            k._sqlite3_bind_text = c.ua;
            k._sqlite3_bind_parameter_index = c.va;
            k._sqlite3_sql = c.wa;
            k._sqlite3_normalized_sql = c.xa;
            k._sqlite3_changes = c.ya;
            k._sqlite3_close_v2 = c.za;
            k._sqlite3_create_function_v2 = c.Aa;
            k._sqlite3_update_hook = c.Ba;
            k._sqlite3_open = c.Ca;
            ca = k._malloc = c.Da;
            da = k._free = c.Ea;
            k._RegisterExtensionFunctions = c.Fa;
            yb = c.Ga;
            Uc = c.Ha;
            ra = c.Ia;
            y = c.Ja;
            pa = c.Ka;
            Ja = c.M;
            Z = c.O;
            Ia();
            K--;
            k.monitorRunDependencies?.(K);
            0 == K && Ua && (c = Ua, Ua = null, c());
            return Xc;
          }
          K++;
          k.monitorRunDependencies?.(K);
          var b = { a: Vc };
          if (k.instantiateWasm) return new Promise((c) => {
            k.instantiateWasm(b, (d, e) => {
              c(a(d, e));
            });
          });
          La ??= k.locateFile ? k.locateFile("sql-wasm-browser.wasm", ya) : ya + "sql-wasm-browser.wasm";
          return a((await Oa(b)).instance);
        })();
        Wc();
        return Module;
      });
      return initSqlJsPromise;
    };
    if (typeof exports === "object" && typeof module2 === "object") {
      module2.exports = initSqlJs2;
      module2.exports.default = initSqlJs2;
    } else if (typeof define === "function" && define["amd"]) {
      define([], function() {
        return initSqlJs2;
      });
    } else if (typeof exports === "object") {
      exports["Module"] = initSqlJs2;
    }
  }
});

// src/plugin.ts
var plugin_exports = {};
__export(plugin_exports, {
  default: () => KnowledgeWorkspacePlugin
});
module.exports = __toCommonJS(plugin_exports);
var import_obsidian5 = require("obsidian");

// src/api/obsidian-api-transport.ts
var import_obsidian = require("obsidian");

// src/api/request-url-transport.ts
var NULL_BODY_STATUS_CODES = /* @__PURE__ */ new Set([204, 205, 304]);
function createRequestUrlTransport(requestUrlFunction) {
  return async (input, init) => {
    const request = new Request(input, init);
    if (request.signal.aborted) {
      throw new DOMException("The request was aborted", "AbortError");
    }
    const headers = {};
    for (const [name, value] of request.headers.entries()) {
      const previousValue = headers[name];
      headers[name] = previousValue === void 0 ? value : `${previousValue}, ${value}`;
    }
    const param = {
      url: request.url,
      method: request.method,
      headers,
      throw: false
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      param.body = await request.arrayBuffer();
    }
    const result = await requestUrlFunction(param);
    const body = NULL_BODY_STATUS_CODES.has(result.status) ? null : result.arrayBuffer;
    return new Response(body, {
      status: result.status,
      headers: result.headers
    });
  };
}
function createRequestUrlSyncTransport(requestUrlFunction) {
  return async (request) => {
    const param = {
      url: request.url,
      method: request.method,
      headers: { ...request.headers },
      throw: false,
      ...request.body === void 0 ? {} : { body: request.body }
    };
    const result = await requestUrlFunction(param);
    return { status: result.status, bodyText: result.text };
  };
}
function createRequestUrlDeviceSyncTransport(requestUrlFunction) {
  return async (request) => {
    const param = {
      url: request.url,
      method: request.method,
      headers: { ...request.headers },
      throw: false,
      ...request.body === void 0 ? {} : { body: request.body }
    };
    const result = await requestUrlFunction(param);
    const headers = {};
    for (const [name, value] of Object.entries(result.headers ?? {})) {
      headers[name.toLowerCase()] = value;
    }
    return {
      status: result.status,
      bodyText: result.text,
      bodyBytes: result.arrayBuffer,
      headers
    };
  };
}
function createRequestUrlPolicyHttpTransport(requestUrlFunction) {
  return async (request) => {
    const result = await requestUrlFunction({
      url: request.url,
      method: "GET",
      headers: { ...request.headers },
      throw: false
    });
    const headers = result.headers ?? {};
    let etag = null;
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === "etag") {
        etag = headers[name] ?? null;
        break;
      }
    }
    return { status: result.status, bodyText: result.text, etag };
  };
}

// src/api/obsidian-api-transport.ts
function createObsidianPolicyHttpTransport() {
  return createRequestUrlPolicyHttpTransport(import_obsidian.requestUrl);
}
function createObsidianSyncHttpTransport() {
  return createRequestUrlSyncTransport(import_obsidian.requestUrl);
}
function createObsidianDeviceSyncHttpTransport() {
  return createRequestUrlDeviceSyncTransport(import_obsidian.requestUrl);
}

// src/authentication/contracts.ts
var CONNECTION_STATUS_TEXT = {
  not_connected: "Not connected",
  requesting_authorization: "Requesting authorization\u2026",
  waiting_for_approval: "Waiting for approval",
  connected: "Connected",
  offline: "Offline \u2014 credentials preserved",
  refresh_required: "Refresh required",
  revoked: "Revoked",
  configuration_invalid: "Configuration invalid"
};
function resolveAuthenticationControls(state, facts) {
  return {
    canLogin: !facts.hasActiveCredential && !facts.hasPendingGrant && state !== "requesting_authorization" && state !== "waiting_for_approval",
    canRetryConnection: state === "offline" && facts.hasActiveCredential,
    canOpenBrowser: facts.hasPendingGrant,
    canCancel: facts.hasPendingGrant,
    canDisconnect: facts.hasActiveCredential
  };
}
var DeviceAuthError = class extends Error {
  code;
  status;
  retryAfterSeconds;
  approvedVersionBounds;
  isLocal;
  constructor(code, options) {
    super(options.message);
    this.name = "DeviceAuthError";
    this.code = code;
    this.status = options.status;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
    this.approvedVersionBounds = options.approvedVersionBounds ?? null;
    this.isLocal = options.isLocal ?? false;
  }
};
function isDeviceAuthError(error) {
  return error instanceof DeviceAuthError;
}
function resolveDeviceAuthClosedCode(error) {
  if (error instanceof DeviceAuthError && error.code !== "") {
    return error.code;
  }
  return null;
}
function parseApprovedVersionBounds(details) {
  const raw = details["approved_version_bounds"];
  if (!Array.isArray(raw) || raw.length < 2) {
    return null;
  }
  const [minimum, maximum] = raw;
  if (typeof minimum !== "string" || typeof maximum !== "string") {
    return null;
  }
  return { minimum, maximum };
}
function mapErrorEnvelope(status, body) {
  const retryAfterRaw = body.details["retry_after_seconds"];
  return new DeviceAuthError(body.code, {
    status,
    message: body.message,
    retryAfterSeconds: typeof retryAfterRaw === "number" ? retryAfterRaw : null,
    approvedVersionBounds: parseApprovedVersionBounds(body.details)
  });
}
function parseEnvelope(status, bodyText) {
  let parsed;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response was not valid JSON",
      isLocal: true
    });
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response was not an envelope object",
      isLocal: true
    });
  }
  const envelope = parsed;
  if (envelope.error !== null && envelope.error !== void 0) {
    throw mapErrorEnvelope(status, envelope.error);
  }
  if (envelope.data === null || envelope.data === void 0) {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response envelope carried no data",
      isLocal: true
    });
  }
  return { data: envelope.data, error: null };
}
function createDeviceApiTransport(http, resolveOrigin) {
  async function post(path, headers, body) {
    const request = {
      url: `${resolveOrigin()}${path}`,
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json", ...headers },
      body: JSON.stringify(body)
    };
    let response;
    try {
      response = await http(request);
    } catch {
      throw new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "the server could not be reached",
        isLocal: true
      });
    }
    return parseEnvelope(response.status, response.bodyText).data;
  }
  return {
    async createGrant(request) {
      return await post("/api/auth/device-authorizations", {}, request);
    },
    async pollGrant(grantId, pollingSecret) {
      return await post(
        `/api/auth/device-authorizations/${encodeURIComponent(grantId)}/poll`,
        { authorization: `Bearer ${pollingSecret}` },
        {}
      );
    },
    async refresh(refreshCredential, rotationId) {
      return await post(
        "/api/auth/device-tokens/refresh",
        { authorization: `Bearer ${refreshCredential}` },
        { rotation_id: rotationId }
      );
    },
    async revokeCurrent(refreshCredential) {
      return await post(
        "/api/auth/device-tokens/revoke-current",
        { authorization: `Bearer ${refreshCredential}` },
        {}
      );
    }
  };
}
var LOOPBACK_HOSTNAME_PATTERN = /^(?:localhost|.+\.localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\])$/;
function parseServerOrigin(origin, options) {
  let url;
  try {
    url = new URL(origin.trim());
  } catch {
    return null;
  }
  if (url.username !== "" || url.password !== "") {
    return null;
  }
  if (url.pathname !== "/" || url.search !== "" || url.hash !== "") {
    return null;
  }
  if (url.protocol === "https:") {
    return url.origin;
  }
  if (url.protocol === "http:" && options.allowLoopbackHttp) {
    if (LOOPBACK_HOSTNAME_PATTERN.test(url.hostname)) {
      return url.origin;
    }
    return null;
  }
  return null;
}
var DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS = 80;
function validateDeviceName(name) {
  const trimmed = name.trim();
  if (trimmed.length === 0 || trimmed.length > DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS) {
    return null;
  }
  return trimmed;
}

// src/exclusion-policy/contracts.ts
var SNAPSHOT_PAYLOAD_CONTRACT = "exclusion_policy_snapshot/v1";
var KEYSET_PAYLOAD_CONTRACT = "exclusion_policy_keyset/v1";
var SNAPSHOT_SIGNING_DOMAIN = "exclusion-policy-snapshot/v1";
var KEYSET_SIGNING_DOMAIN = "exclusion-policy-keyset/v1";
var SIGNED_SNAPSHOT_MAXIMUM_BYTES = 256 * 1024;
var KEYSET_MAXIMUM_NON_RETIRED_KEYS = 4;
var KEYSET_PAGE_MAXIMUM_FETCHES = 8;
var MAXIMUM_RULES_PER_REVISION = 256;
var EVALUATOR_CONTRACT = "exclusion_policy_evaluator/v1";
var ED25519_PUBLIC_KEY_BYTES = 32;
var ED25519_SIGNATURE_BYTES = 64;
var SIGNATURE_ALGORITHM = "Ed25519";
var KEY_ID_PREFIX = "ed25519-sha256-";
var PolicyVerificationError = class extends Error {
  reason;
  constructor(reason, message) {
    super(message);
    this.name = "PolicyVerificationError";
    this.reason = reason;
  }
};
function policyVerificationError(reason) {
  return new PolicyVerificationError(reason, `exclusion policy verification failed: ${reason}`);
}
var SOURCE_TYPES = [
  "markdown",
  "text",
  "pdf",
  "image",
  "audio",
  "web",
  "youtube"
];

// src/authentication/secret-storage-record.ts
var DEVICE_CREDENTIAL_RECORD_NAME = "knowledge-workspace-device-credential";
var SECRET_RECORD_NAME_PATTERN = /^[a-z0-9-]+$/;
function isSecretRecordNameValid(recordName) {
  return SECRET_RECORD_NAME_PATTERN.test(recordName);
}
var CLEARED_REASONS = [
  "grant_denied",
  "grant_expired",
  "login_cancelled",
  "grant_invalid",
  "token_reuse",
  "credential_invalid",
  "device_revoked",
  "self_disconnect"
];
var RECORD_VERSION = 1;
function isClearedReason(value) {
  return typeof value === "string" && CLEARED_REASONS.includes(value);
}
function parseDeviceSecretRecord(value) {
  if (typeof value !== "string") {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const candidate = parsed;
  if (candidate["record_version"] !== RECORD_VERSION) {
    return null;
  }
  if (candidate["state"] === "pending_grant") {
    if (typeof candidate["polling_secret"] !== "string") {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "pending_grant",
      polling_secret: candidate["polling_secret"]
    };
  }
  if (candidate["state"] === "active") {
    if (typeof candidate["refresh_credential"] !== "string") {
      return null;
    }
    if (typeof candidate["refresh_generation"] !== "number") {
      return null;
    }
    const pendingRotationId = candidate["pending_rotation_id"];
    if (pendingRotationId !== null && typeof pendingRotationId !== "string") {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "active",
      refresh_credential: candidate["refresh_credential"],
      refresh_generation: candidate["refresh_generation"],
      pending_rotation_id: pendingRotationId
    };
  }
  if (candidate["state"] === "cleared") {
    if (!isClearedReason(candidate["cleared_reason"])) {
      return null;
    }
    return {
      record_version: RECORD_VERSION,
      state: "cleared",
      cleared_reason: candidate["cleared_reason"]
    };
  }
  return null;
}
function readDeviceSecretRecord(store, recordName) {
  return parseDeviceSecretRecord(store.getSecret(recordName));
}
function writeVerifiedRecord(store, recordName, record) {
  const serialized = JSON.stringify(record);
  store.setSecret(recordName, serialized);
  if (store.getSecret(recordName) !== serialized) {
    throw new DeviceAuthError("secret_storage_unverified", {
      status: 0,
      message: "the credential record could not be verified after writing",
      isLocal: true
    });
  }
}
function writePendingGrantRecord(store, recordName, pollingSecret) {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "pending_grant",
    polling_secret: pollingSecret
  });
}
function writeActiveDeviceRecord(store, recordName, fields) {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "active",
    refresh_credential: fields.refresh_credential,
    refresh_generation: fields.refresh_generation,
    pending_rotation_id: fields.pending_rotation_id
  });
}
function writeClearedTombstone(store, recordName, clearedReason) {
  writeVerifiedRecord(store, recordName, {
    record_version: RECORD_VERSION,
    state: "cleared",
    cleared_reason: clearedReason
  });
}

// src/authentication/device-authorization.ts
var DeviceAuthorizationController = class {
  #deps;
  #stopRequested = false;
  constructor(deps) {
    this.#deps = deps;
  }
  /** Stop the poll loop without touching any record (plugin unload). */
  stop() {
    this.#stopRequested = true;
  }
  /**
   * Start one bounded onboarding: create the grant, persist the polling
   * secret, open the verification URL, then poll until a terminal outcome,
   * expiry or a recoverable offline state.
   */
  async login() {
    this.#stopRequested = false;
    const existingRecord = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (existingRecord?.state === "active") {
      this.#deps.onStateChange("refresh_required", "device_credential_invalid");
      throw new DeviceAuthError("device_credential_invalid", {
        status: 0,
        message: "an active device credential record exists; disconnect before starting a new login",
        isLocal: true
      });
    }
    const origin = parseServerOrigin(this.#deps.settings.server_origin, {
      allowLoopbackHttp: this.#deps.allowLoopbackHttp
    });
    const deviceName = validateDeviceName(this.#deps.settings.device_name);
    if (origin === null || deviceName === null) {
      this.#deps.onStateChange(
        "configuration_invalid",
        origin === null ? "the server origin must be an exact HTTPS origin" : null
      );
      return;
    }
    this.#deps.onStateChange("requesting_authorization", null);
    let grant;
    try {
      grant = await this.#deps.transport.createGrant({
        client_instance_id: this.#deps.clientIdentity.clientInstanceId,
        device_name: deviceName,
        platform_class: this.#deps.clientIdentity.platformClass,
        platform_name: this.#deps.clientIdentity.platformName,
        plugin_version: this.#deps.clientIdentity.pluginVersion,
        requested_scope: "obsidian_sync"
      });
    } catch (error) {
      this.#surfaceCreationFailure(error);
      return;
    }
    writePendingGrantRecord(this.#deps.secretStore, this.#deps.recordName, grant.polling_secret);
    this.#deps.settings.pending_grant = {
      grant_id: grant.grant_id,
      user_code: grant.user_code,
      verification_uri: grant.verification_uri,
      expires_at_epoch_seconds: Math.floor(this.#deps.nowEpochMs() / 1e3) + grant.expires_in_seconds,
      poll_interval_seconds: grant.poll_interval_seconds
    };
    this.#deps.settings.secret_record_name = this.#deps.recordName;
    await this.#deps.persistSettings();
    this.#deps.onStateChange("waiting_for_approval", grant.user_code);
    this.#deps.openUrl(grant.verification_uri_complete);
    await this.#pollUntilTerminal();
  }
  /** Re-open the approval page for a still-pending grant (spec 11.2). */
  openBrowserAgain() {
    const pendingGrant = this.#deps.settings.pending_grant;
    if (pendingGrant === null) {
      return;
    }
    this.#deps.openUrl(`${pendingGrant.verification_uri}#${pendingGrant.user_code}`);
  }
  /**
   * Cancel a pending login: stop polling, tombstone the record locally and
   * clear the non-secret settings reference. No network call is made. A
   * stale reference over an already-exchanged record is dropped without
   * touching the live credential.
   */
  async cancelPendingLogin() {
    this.#stopRequested = true;
    if (this.#deps.settings.pending_grant === null) {
      return;
    }
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state === "active") {
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = this.#deps.recordName;
      await this.#deps.persistSettings();
      this.#deps.onStateChange("connected", null);
      return;
    }
    await this.#terminatePendingGrant("login_cancelled", "not_connected");
  }
  /**
   * Reconcile the crash-window pairings between the record and the pending
   * grant reference (spec 19 bounded startup), locally and without network:
   * a poll exchange commits the active record before the pending reference
   * is cleared, and a tombstone commits before the reference is cleared, so
   * a crash in either gap leaves a stale reference that must never destroy
   * the committed record. An active record keeps its credential and reports
   * requires a refresh before it can report connected; a cleared (or absent)
   * record drops the stale reference and
   * reports not_connected. Pending records belong to the resume path.
   */
  async reconcileCrashWindow() {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state === "pending_grant") {
      return;
    }
    if (record?.state === "active") {
      const referenceIsStale2 = this.#deps.settings.pending_grant !== null || this.#deps.settings.secret_record_name !== this.#deps.recordName;
      if (referenceIsStale2) {
        this.#deps.settings.pending_grant = null;
        this.#deps.settings.secret_record_name = this.#deps.recordName;
        await this.#deps.persistSettings();
      }
      this.#deps.onStateChange("refresh_required", null);
      return;
    }
    const referenceIsStale = this.#deps.settings.pending_grant !== null || this.#deps.settings.secret_record_name !== null;
    if (referenceIsStale) {
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = null;
      await this.#deps.persistSettings();
      this.#deps.onStateChange("not_connected", null);
    }
  }
  /**
   * Resume one still-pending grant after a restart (spec 19): an expired
   * grant is tombstoned locally without polling; an unexpired grant resumes
   * the bounded poll loop with its persisted interval.
   */
  async resumePendingGrant() {
    const pendingGrant = this.#deps.settings.pending_grant;
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (pendingGrant === null || record?.state !== "pending_grant") {
      if (record?.state === "pending_grant") {
        await this.#terminatePendingGrant("grant_invalid", "not_connected");
      }
      return;
    }
    if (this.#deps.nowEpochMs() >= pendingGrant.expires_at_epoch_seconds * 1e3) {
      await this.#terminatePendingGrant("grant_expired", "not_connected");
      return;
    }
    this.#stopRequested = false;
    this.#deps.onStateChange("waiting_for_approval", pendingGrant.user_code);
    await this.#pollUntilTerminal();
  }
  async #terminatePendingGrant(clearedReason, nextState) {
    writeClearedTombstone(this.#deps.secretStore, this.#deps.recordName, clearedReason);
    this.#deps.settings.pending_grant = null;
    this.#deps.settings.secret_record_name = null;
    await this.#deps.persistSettings();
    this.#deps.onStateChange(nextState, clearedReason);
  }
  #surfaceCreationFailure(error) {
    if (isDeviceAuthError(error)) {
      if (error.code === "authentication_rate_limited") {
        const retryGuidance = error.retryAfterSeconds !== null && error.retryAfterSeconds > 0 ? ` \u2014 retry login after ${error.retryAfterSeconds} seconds` : "";
        this.#deps.onStateChange(
          "not_connected",
          `authentication_rate_limited${retryGuidance}`
        );
        return;
      }
      if (error.code === "plugin_version_unsupported" || error.code === "api_request_validation_failed" || error.code === "api_request_malformed") {
        const detail = error.approvedVersionBounds === null ? null : `approved plugin versions ${error.approvedVersionBounds.minimum} \u2013 ${error.approvedVersionBounds.maximum}`;
        this.#deps.onStateChange("configuration_invalid", detail);
        return;
      }
    }
    this.#deps.onStateChange("offline", resolveDeviceAuthClosedCode(error));
  }
  async #pollUntilTerminal() {
    const pendingGrant = this.#deps.settings.pending_grant;
    if (pendingGrant === null) {
      return;
    }
    let intervalSeconds = pendingGrant.poll_interval_seconds;
    for (; ; ) {
      await this.#deps.delay(intervalSeconds * 1e3);
      if (this.#stopRequested) {
        return;
      }
      const currentGrant = this.#deps.settings.pending_grant;
      if (currentGrant === null) {
        return;
      }
      if (this.#deps.nowEpochMs() >= currentGrant.expires_at_epoch_seconds * 1e3) {
        await this.#terminatePendingGrant("grant_expired", "not_connected");
        return;
      }
      const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
      if (record?.state !== "pending_grant") {
        return;
      }
      let exchange;
      try {
        exchange = await this.#deps.transport.pollGrant(
          currentGrant.grant_id,
          record.polling_secret
        );
      } catch (error) {
        const outcome = this.#classifyPollFailure(error);
        if (outcome.kind === "continue") {
          if (outcome.intervalSeconds !== void 0) {
            intervalSeconds = outcome.intervalSeconds;
          }
          continue;
        }
        if (outcome.kind === "terminal") {
          await this.#terminatePendingGrant(outcome.clearedReason, "not_connected");
          return;
        }
        this.#deps.onStateChange("offline", outcome.code);
        return;
      }
      if (this.#stopRequested || this.#deps.settings.pending_grant === null) {
        if (this.#deps.settings.pending_grant === null) {
          this.#deps.onStateChange("not_connected", null);
        }
        return;
      }
      writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
        refresh_credential: exchange.refresh_credential,
        refresh_generation: exchange.refresh_generation,
        pending_rotation_id: null
      });
      this.#deps.settings.pending_grant = null;
      this.#deps.settings.secret_record_name = this.#deps.recordName;
      await this.#deps.persistSettings();
      try {
        await this.#deps.onExchange(exchange);
      } catch (error) {
        const policyReason = error instanceof PolicyVerificationError ? error.reason : null;
        this.#deps.onStateChange("offline", policyReason);
        return;
      }
      this.#deps.onStateChange("connected", null);
      return;
    }
  }
  /**
   * Classify one poll failure without side effects: pending and slow-down
   * carry the server's exact retry hint, terminal outcomes name their
   * tombstone reason, and everything recoverable is an offline finish that
   * preserves the record while carrying the closed code it closed on
   * (closed-reason surfacing C2 A5).
   */
  #classifyPollFailure(error) {
    if (isDeviceAuthError(error)) {
      if (error.code === "device_authorization_pending" || error.code === "device_authorization_slow_down") {
        const retryHint = error.retryAfterSeconds;
        if (retryHint === null || retryHint < 1) {
          return { kind: "continue" };
        }
        return { kind: "continue", intervalSeconds: retryHint };
      }
      if (error.code === "device_authorization_denied") {
        return { kind: "terminal", clearedReason: "grant_denied" };
      }
      if (error.code === "device_authorization_expired") {
        return { kind: "terminal", clearedReason: "grant_expired" };
      }
      if (error.code === "device_credential_invalid" || error.code === "device_authorization_state_invalid") {
        return { kind: "terminal", clearedReason: "grant_invalid" };
      }
    }
    return { kind: "offline", code: resolveDeviceAuthClosedCode(error) };
  }
};

// src/authentication/settings-tab.ts
var import_obsidian2 = require("obsidian");

// src/device-sync/status.ts
var DEVICE_SYNC_REPAIR_STATE_TEXT = {
  ready: "Ready",
  required: "Required",
  running: "Running",
  blocked: "Blocked"
};
function projectDeviceSyncStatus(input) {
  const { state } = input;
  const isRepairOwed = state.barrierGeneration !== null || state.activeManifestRunId !== null || input.isJournalReconcileRequired;
  const repairState = input.blockedRepairReason !== null && isRepairOwed ? "blocked" : input.isRepairRunning && isRepairOwed ? "running" : isRepairOwed ? "required" : "ready";
  let pendingActionCount = 0;
  let settledReason = null;
  for (const action of input.manifestActions) {
    if (action.outcome !== "terminal_safe" || action.reason !== null) {
      pendingActionCount += 1;
    }
    if (action.outcome === "terminal_safe" && action.reason !== null) {
      settledReason = action.reason;
    }
  }
  const reason = (input.blockedRepairReason !== null && isRepairOwed ? input.blockedRepairReason : null) ?? state.barrierReason ?? settledReason;
  const deliveredOrCheckpointSequence = Math.max(
    state.appliedSequence,
    state.manifestCheckpointSequence ?? 0
  );
  const cursorLag = Math.max(0, deliveredOrCheckpointSequence - state.acknowledgedSequence);
  return {
    appliedSequence: state.appliedSequence,
    acknowledgedSequence: state.acknowledgedSequence,
    cursorLag,
    repairState,
    reason,
    pendingActionCount
  };
}
function renderDeviceSyncStatusText(status) {
  const stateLabel = DEVICE_SYNC_REPAIR_STATE_TEXT[status.repairState];
  const reasonText = status.reason === null ? "" : ` (${status.reason})`;
  return [
    `Repair: ${stateLabel}${reasonText}`,
    `Applied: ${status.appliedSequence}`,
    `Acknowledged: ${status.acknowledgedSequence}`,
    `Cursor lag: ${status.cursorLag}`,
    `Pending actions: ${status.pendingActionCount}`
  ].join(" \xB7 ");
}

// src/journal/sqlite-database.ts
var import_sql = __toESM(require_sql_wasm_browser());

// src/conflicts/contracts.ts
var CONFLICT_KINDS = [
  "stale_content",
  "edit_remote_delete",
  "delete_remote_edit",
  "locator_collision"
];
var CONFLICT_STATUSES = [
  "open",
  "resolving",
  "resolved",
  "superseded"
];
var CONFLICT_CANDIDATE_KINDS = [
  "content",
  "delete"
];
var CONFLICT_RESOLUTION_KINDS = [
  "keep_remote",
  "keep_local",
  "save_merged"
];
var CONFLICT_RESOLUTION_OUTCOMES = [
  "resolved",
  "stale_successor"
];
var CONFLICT_EVIDENCE_ROLES = [
  "base",
  "remote",
  "candidate"
];
function isConflictEvidenceRole(value) {
  return typeof value === "string" && CONFLICT_EVIDENCE_ROLES.includes(value);
}
var ConflictContractError = class extends Error {
  reason;
  constructor() {
    super("conflict contract invalid");
    this.name = "ConflictContractError";
    this.reason = "conflict_contract_invalid";
  }
};
function contractInvalid() {
  return new ConflictContractError();
}
var UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function isRecord(value) {
  return typeof value === "object" && value !== null;
}
function requireRecord(value) {
  if (!isRecord(value) || Array.isArray(value)) {
    throw contractInvalid();
  }
  return value;
}
function decodeClosedToken(value, closedSet) {
  if (typeof value !== "string" || !closedSet.includes(value)) {
    throw contractInvalid();
  }
  return value;
}
function decodeUuid(value) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw contractInvalid();
  }
  return value;
}
function decodeNullableUuid(value) {
  if (value === null) {
    return null;
  }
  return decodeUuid(value);
}
function decodeTimestamp(value) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw contractInvalid();
  }
  return value;
}
function decodeNullableTimestamp(value) {
  if (value === null) {
    return null;
  }
  return decodeTimestamp(value);
}
function decodeResolutionKind(value) {
  return decodeClosedToken(value, CONFLICT_RESOLUTION_KINDS);
}
function decodeConflictSummary(value) {
  const data = requireRecord(value);
  const summary = {
    conflictId: decodeUuid(data["conflict_id"]),
    sourceId: decodeNullableUuid(data["source_id"]),
    conflictKind: decodeClosedToken(data["conflict_kind"], CONFLICT_KINDS),
    status: decodeClosedToken(data["status"], CONFLICT_STATUSES),
    originatingEventId: decodeUuid(data["originating_event_id"]),
    originatingDeviceId: decodeUuid(data["originating_device_id"]),
    baseVersionId: decodeNullableUuid(data["base_version_id"]),
    observedRemoteVersionId: decodeNullableUuid(data["observed_remote_version_id"]),
    candidateKind: decodeClosedToken(data["candidate_kind"], CONFLICT_CANDIDATE_KINDS),
    verifiedCandidateObjectId: decodeNullableUuid(data["verified_candidate_object_id"]),
    capturedAt: decodeTimestamp(data["captured_at"]),
    resolutionKind: data["resolution_kind"] === null ? null : decodeResolutionKind(data["resolution_kind"]),
    resolutionEventId: decodeNullableUuid(data["resolution_event_id"]),
    resultingVersionId: decodeNullableUuid(data["resulting_version_id"]),
    successorConflictId: decodeNullableUuid(data["successor_conflict_id"]),
    closedAt: decodeNullableTimestamp(data["closed_at"])
  };
  return summary;
}
function decodeConflictDetail(value) {
  const data = requireRecord(value);
  const choicesWire = data["choices"];
  if (!Array.isArray(choicesWire)) {
    throw contractInvalid();
  }
  const choices = choicesWire.map((choice) => decodeResolutionKind(choice));
  return { ...decodeConflictSummary(data), choices };
}
function decodeConflictPage(value) {
  const data = requireRecord(value);
  const conflictsWire = data["conflicts"];
  if (!Array.isArray(conflictsWire)) {
    throw contractInvalid();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw contractInvalid();
  }
  return {
    conflicts: conflictsWire.map((conflict) => decodeConflictSummary(conflict)),
    hasMore,
    nextExclusiveStartConflictId: decodeNullableUuid(data["next_exclusive_start_conflict_id"])
  };
}
function decodeConflictResolution(value) {
  const data = requireRecord(value);
  return {
    outcome: decodeClosedToken(data["outcome"], CONFLICT_RESOLUTION_OUTCOMES),
    conflictId: decodeUuid(data["conflict_id"]),
    resolutionEventId: decodeUuid(data["resolution_event_id"]),
    resolutionKind: decodeResolutionKind(data["resolution_kind"]),
    resultingVersionId: decodeNullableUuid(data["resulting_version_id"]),
    successorConflictId: decodeNullableUuid(data["successor_conflict_id"]),
    completedAt: decodeTimestamp(data["completed_at"])
  };
}
var IDEMPOTENCY_KEY_PATTERN = UUID_PATTERN;
function validateConflictResolveInput(input) {
  const resolutionKind = decodeClosedToken(input.resolutionKind, CONFLICT_RESOLUTION_KINDS);
  const reviewedRemoteVersionId = input.reviewedRemoteVersionId ?? null;
  const verifiedCandidateObjectId = input.verifiedCandidateObjectId ?? null;
  if (!UUID_PATTERN.test(input.conflictId) || !UUID_PATTERN.test(input.resolutionEventId) || typeof input.idempotencyKey !== "string" || !IDEMPOTENCY_KEY_PATTERN.test(input.idempotencyKey) || reviewedRemoteVersionId !== null && !UUID_PATTERN.test(reviewedRemoteVersionId) || verifiedCandidateObjectId !== null && !UUID_PATTERN.test(verifiedCandidateObjectId)) {
    throw contractInvalid();
  }
  if (resolutionKind === "save_merged") {
    if (verifiedCandidateObjectId === null) {
      throw contractInvalid();
    }
    return;
  }
  if (verifiedCandidateObjectId !== null) {
    throw contractInvalid();
  }
}
var CONFLICT_LOCAL_REPAIR_ACTIONS = [
  "apply_remote_version",
  "apply_resulting_version",
  "apply_remote_tombstone"
];
var CONFLICT_LOCAL_REPAIR_SAFE_REASONS = [
  "resolution_committed",
  "winner_download_failed",
  "vault_apply_failed"
];

// src/journal/contracts.ts
var MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024;
var MAX_MULTIPART_FILE_SIZE_BYTES = 100 * 1024 * 1024;
var MAX_PENDING_EVENTS = 1e4;
var MAX_JOURNAL_SIZE_BYTES = 64 * 1024 * 1024;
var FILE_SETTLE_DELAY_MS = 250;
var MULTIPART_PART_SIZE_BYTES = 8 * 1024 * 1024;
var MAX_MULTIPART_PART_COUNT = 13;
var MAX_EVENT_ATTEMPT_HISTORY = 10;
var JOURNAL_EVENT_STATES = [
  "queued",
  "preflight",
  "uploading",
  "committed",
  "no_change",
  "waiting_retry",
  "excluded_policy",
  "blocked_size",
  "blocked_conflict",
  "deferred_lifecycle",
  "integrity_failed"
];
var JOURNAL_PENDING_EVENT_STATES = [
  "queued",
  "preflight",
  "uploading",
  "waiting_retry"
];
var JOURNAL_COALESCABLE_EVENT_STATES = [
  "queued",
  "waiting_retry"
];
var JOURNAL_NON_RETRY_EVENT_STATES = [
  "excluded_policy",
  "blocked_size",
  "blocked_conflict",
  "deferred_lifecycle",
  "integrity_failed"
];
var JOURNAL_SAFE_ERROR_LABELS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "login_required",
  "excluded_policy",
  "blocked_size",
  "blocked_conflict",
  "deferred_lifecycle",
  "integrity_failed",
  "multipart_local_content_changed",
  "reconcile_required",
  "committed"
];
var MULTIPART_SESSION_STATES = [
  "created",
  "uploading",
  "completing",
  "verifying",
  "promoting",
  "committed",
  "cancelling",
  "expired",
  "integrity_failed",
  "policy_denied",
  "cleanup_pending",
  "cleaned"
];
var MULTIPART_SAFE_REASON_TOKENS = [
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid",
  "multipart_part_invalid",
  "multipart_part_url_rejected",
  "multipart_provider_state_invalid",
  "multipart_completion_in_progress",
  "multipart_integrity_failed",
  "multipart_policy_denied",
  "multipart_cleanup_failed",
  "multipart_local_content_changed",
  "multipart_dependency_unavailable"
];
var JOURNAL_RECOVERY_STATES = [
  "fresh_journal_created",
  "fresh_journal_reconcile_required",
  "verified_generation_loaded",
  "prior_generation_recovered",
  "empty_journal_rebuilt"
];
var JOURNAL_CAPTURE_ADMISSIONS = [
  "policy_allowed",
  "blocked_size",
  "excluded_policy"
];
var JOURNAL_OPERATIONS = [
  "create",
  "update",
  "rename",
  "move",
  "delete",
  "restore"
];

// src/journal/sqlite-database.ts
var JOURNAL_SCHEMA_VERSION = 10;
var JOURNAL_STORE_ERROR_REASONS = [
  "journal_schema_unsupported",
  "journal_image_invalid",
  "journal_mutation_failed",
  "journal_query_failed",
  "journal_store_unavailable",
  "journal_generation_write_failed",
  "journal_manifest_invalid",
  "journal_not_open"
];
var JournalStoreError = class extends Error {
  reason;
  constructor(reason, message) {
    super(message);
    this.name = "JournalStoreError";
    this.reason = reason;
  }
};
function journalStoreError(reason) {
  return new JournalStoreError(reason, `journal store failed: ${reason}`);
}
var loadVendoredSqliteEngine = (options) => (0, import_sql.default)(options);
var JOURNAL_META_DDL = `
create table if not exists journal_meta (
  singleton_key integer primary key check (singleton_key = 1),
  schema_version integer not null,
  dirty_generation integer not null,
  last_verified_generation integer not null,
  is_reconcile_required integer not null check (is_reconcile_required in (0, 1)),
  recovery_state text not null
);
`;
var LOCAL_FILES_DDL = `
create table if not exists local_files (
  local_file_id text primary key,
  normalized_path text not null unique,
  source_id text,
  observed_sha256 text not null,
  observed_size_bytes integer not null check (observed_size_bytes >= 0),
  observed_media_type text not null,
  base_version_id text,
  policy_revision integer not null check (policy_revision >= 0),
  last_locator text,
  open_tombstone_id text,
  lifecycle_state text not null default 'active'
    check (lifecycle_state in ('active', 'rename_pending', 'move_pending',
      'delete_pending', 'restore_pending', 'tombstoned', 'restored',
      'reconcile_required')),
  last_committed_sha256 text,
  last_committed_size_bytes integer check (last_committed_size_bytes >= 0),
  last_committed_media_type text,
  restore_prior_path text
);
`;
var JOURNAL_EVENTS_DDL = `
create table if not exists journal_events (
  event_id text primary key,
  local_file_id text not null references local_files (local_file_id),
  idempotency_key text not null unique,
  operation text not null check (operation in ('create', 'update',
    'rename', 'move', 'delete', 'restore')),
  sha256 text not null,
  size_bytes integer not null check (size_bytes >= 0),
  media_type text not null,
  state text not null check (state in ('queued', 'preflight', 'uploading', 'committed',
    'no_change', 'waiting_retry', 'excluded_policy', 'blocked_size', 'blocked_conflict',
    'deferred_lifecycle', 'integrity_failed')),
  is_fingerprint_frozen integer not null check (is_fingerprint_frozen in (0, 1)),
  attempt_count integer not null check (attempt_count >= 0),
  next_eligible_retry_epoch_ms integer,
  safe_error text,
  operation_id text,
  created_at_epoch_ms integer not null check (created_at_epoch_ms >= 0)
);
create index if not exists journal_events_file_created_idx
  on journal_events (local_file_id, created_at_epoch_ms);
create index if not exists journal_events_state_idx on journal_events (state);
`;
var JOURNAL_ATTEMPTS_DDL = `
create table if not exists journal_attempts (
  attempt_ordinal integer primary key autoincrement,
  event_id text not null references journal_events (event_id),
  attempted_at_epoch_ms integer not null check (attempted_at_epoch_ms >= 0),
  outcome_label text not null,
  request_correlation_id text not null
);
create index if not exists journal_attempts_event_idx
  on journal_attempts (event_id, attempt_ordinal);
`;
var LIFECYCLE_EVENT_OPERANDS_DDL = `
create table if not exists lifecycle_event_operands (
  event_id text primary key references journal_events (event_id),
  source_id text not null,
  expected_version_id text not null,
  expected_locator text,
  target_locator text,
  tombstone_id text,
  policy_revision integer not null check (policy_revision >= 1),
  predecessor_event_id text references journal_events (event_id),
  server_receipt_tombstone_id text
);
create index if not exists lifecycle_operands_predecessor_idx
  on lifecycle_event_operands (predecessor_event_id);
`;
var DEVICE_SYNC_STATE_DDL = `
create table if not exists device_sync_state (
  singleton_key integer primary key check (singleton_key = 1),
  applied_sequence integer not null check (applied_sequence >= 0),
  acknowledged_sequence integer not null check (acknowledged_sequence >= 0
    and acknowledged_sequence <= applied_sequence),
  observation_generation integer not null check (observation_generation >= 0),
  barrier_generation integer check (barrier_generation >= 0),
  barrier_reason text,
  active_manifest_run_id text,
  manifest_checkpoint_sequence integer check (manifest_checkpoint_sequence >= 0),
  manifest_final_digest text
);
`;
var MANIFEST_PAGE_PROGRESS_DDL = `
create table if not exists manifest_page_progress (
  manifest_run_id text not null,
  page_number integer not null check (page_number >= 0),
  entry_count integer not null check (entry_count >= 0),
  page_digest text not null,
  primary key (manifest_run_id, page_number)
);
`;
var MANIFEST_ACTION_PROGRESS_DDL = `
create table if not exists manifest_action_progress (
  manifest_run_id text not null,
  action_index integer not null check (action_index >= 0),
  action_kind text not null check (action_kind in ('upload', 'download',
    'apply_tombstone', 'conflict', 'no_change', 'excluded')),
  outcome text not null check (outcome in ('received', 'terminal_safe')),
  safe_reason_code text,
  primary key (manifest_run_id, action_index)
);
`;
var REMOTE_APPLY_OPERATIONS_DDL = `
create table if not exists remote_apply_operations (
  event_sequence integer primary key check (event_sequence >= 1),
  event_id text not null,
  source_id text not null,
  operation text not null check (operation in ('created', 'updated',
    'renamed', 'moved', 'deleted', 'restored')),
  prior_locator text,
  target_locator text,
  base_sha256 text,
  base_size_bytes integer check (base_size_bytes >= 0),
  base_media_type text,
  final_sha256 text,
  final_size_bytes integer check (final_size_bytes >= 0),
  final_media_type text,
  temp_token text,
  rollback_token text,
  state text not null check (state in ('prepared', 'temp_verified',
    'vault_mutated', 'locally_applied', 'server_acknowledged')),
  safe_error_code text
);
`;
var ECHO_MARKERS_DDL = `
create table if not exists echo_markers (
  event_sequence integer primary key check (event_sequence >= 1),
  source_id text not null,
  operation text not null check (operation in ('created', 'updated',
    'renamed', 'moved', 'deleted', 'restored')),
  prior_locator text,
  target_locator text,
  final_sha256 text,
  final_size_bytes integer check (final_size_bytes >= 0),
  final_media_type text
);
`;
var DEVICE_SYNC_STATE_SEED_SQL = "insert into device_sync_state (singleton_key, applied_sequence, acknowledged_sequence, observation_generation) values (1, 0, 0, 0);";
function sqlTextList(values) {
  return values.map((value) => `'${value}'`).join(", ");
}
var MULTIPART_UPLOAD_PROGRESS_DDL = `
create table if not exists multipart_upload_progress (
  event_id text primary key references journal_events (event_id),
  session_id text not null,
  part_size_bytes integer not null check (part_size_bytes = ${MULTIPART_PART_SIZE_BYTES}),
  part_count integer not null check (part_count >= 1
    and part_count <= ${MAX_MULTIPART_PART_COUNT}),
  expires_at_epoch_ms integer not null check (expires_at_epoch_ms >= 0),
  completed_part_numbers_json text not null,
  session_state text not null check (session_state in (${sqlTextList(MULTIPART_SESSION_STATES)})),
  safe_reason text check (safe_reason is null
    or safe_reason in (${sqlTextList(MULTIPART_SAFE_REASON_TOKENS)}))
);
`;
var CONFLICT_LOCAL_REPAIRS_DDL = `
create table if not exists conflict_local_repairs (
  conflict_id text primary key,
  resolution_event_id text not null,
  target_action text not null check (target_action in (${sqlTextList(CONFLICT_LOCAL_REPAIR_ACTIONS)})),
  safe_reason text not null check (safe_reason in (${sqlTextList(CONFLICT_LOCAL_REPAIR_SAFE_REASONS)})),
  attempt_count integer not null check (attempt_count >= 0),
  next_eligible_retry_epoch_ms integer check (next_eligible_retry_epoch_ms >= 0),
  created_at_epoch_ms integer not null check (created_at_epoch_ms >= 0),
  updated_at_epoch_ms integer not null check (updated_at_epoch_ms >= 0)
);
`;
var PENDING_RENAME_INTENTS_DDL = `
create table if not exists pending_rename_intents (
  local_file_id text primary key
    references local_files (local_file_id) on delete cascade,
  prior_path text not null check (length(prior_path) > 0),
  current_path text not null check (length(current_path) > 0)
);
create unique index if not exists pending_rename_intents_current_path_uq
  on pending_rename_intents (current_path);
`;
var PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL = `
create table if not exists pending_rename_intent_missing_file_deferrals (
  local_file_id text primary key
    references pending_rename_intents (local_file_id) on delete cascade,
  event_id text not null unique references journal_events (event_id),
  deferred_attempt_count integer not null
    check (deferred_attempt_count between 1 and 40)
);
`;
var JOURNAL_SCHEMA_DDL = [
  JOURNAL_META_DDL,
  LOCAL_FILES_DDL,
  JOURNAL_EVENTS_DDL,
  JOURNAL_ATTEMPTS_DDL,
  LIFECYCLE_EVENT_OPERANDS_DDL,
  DEVICE_SYNC_STATE_DDL,
  MANIFEST_PAGE_PROGRESS_DDL,
  MANIFEST_ACTION_PROGRESS_DDL,
  REMOTE_APPLY_OPERATIONS_DDL,
  ECHO_MARKERS_DDL,
  MULTIPART_UPLOAD_PROGRESS_DDL,
  CONFLICT_LOCAL_REPAIRS_DDL,
  PENDING_RENAME_INTENTS_DDL,
  PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL
].join("");
var RESTORE_RESERVATION_SCHEMA_VERSION = 6;
var DEVICE_SYNC_SCHEMA_VERSION = 7;
var MULTIPART_PROGRESS_SCHEMA_VERSION = 8;
var CONFLICT_REPAIR_SCHEMA_VERSION = 9;
function isJournalRecoveryState(value) {
  return typeof value === "string" && JOURNAL_RECOVERY_STATES.includes(value);
}
function isNonNegativeInteger(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function validateJournalMeta(meta) {
  if (meta.schemaVersion !== JOURNAL_SCHEMA_VERSION) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger(meta.dirtyGeneration) || !isNonNegativeInteger(meta.lastVerifiedGeneration)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof meta.isReconcileRequired !== "boolean" || !isJournalRecoveryState(meta.recoveryState)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function journalMetaWriteSql(meta) {
  validateJournalMeta(meta);
  const reconcileFlag = meta.isReconcileRequired ? 1 : 0;
  return [
    "update journal_meta set",
    `schema_version = ${meta.schemaVersion},`,
    `dirty_generation = ${meta.dirtyGeneration},`,
    `last_verified_generation = ${meta.lastVerifiedGeneration},`,
    `is_reconcile_required = ${reconcileFlag},`,
    `recovery_state = '${meta.recoveryState}'`,
    "where singleton_key = 1;"
  ].join(" ");
}
function parseJournalMetaRow(row) {
  const [
    schemaVersion,
    dirtyGeneration,
    lastVerifiedGeneration,
    isReconcileRequired,
    recoveryState
  ] = row;
  const meta = {
    schemaVersion,
    dirtyGeneration,
    lastVerifiedGeneration,
    isReconcileRequired: isReconcileRequired === 1,
    recoveryState
  };
  validateJournalMeta(meta);
  return meta;
}
var SqliteDatabase = class _SqliteDatabase {
  #engine;
  #mutationTail = Promise.resolve();
  constructor(engine) {
    this.#engine = engine;
  }
  /** Create an empty journal image stamped with the current schema version. */
  static createEmpty(engineModule, initialMeta) {
    validateJournalMeta(initialMeta);
    const database = new _SqliteDatabase(new engineModule.Database());
    try {
      database.#engine.exec(JOURNAL_SCHEMA_DDL);
      database.#engine.exec(
        [
          "insert into journal_meta (singleton_key, schema_version, dirty_generation,",
          "last_verified_generation, is_reconcile_required, recovery_state) values (1,",
          `${initialMeta.schemaVersion}, ${initialMeta.dirtyGeneration},`,
          `${initialMeta.lastVerifiedGeneration},`,
          `${initialMeta.isReconcileRequired ? 1 : 0}, '${initialMeta.recoveryState}');`
        ].join(" ")
      );
      database.#engine.exec(DEVICE_SYNC_STATE_SEED_SQL);
      database.#engine.exec(`pragma user_version = ${JOURNAL_SCHEMA_VERSION};`);
      return database;
    } catch {
      database.close();
      throw journalStoreError("journal_mutation_failed");
    }
  }
  /**
   * Open a persisted journal image. The image must carry exactly the schema
   * version this build understands: an older or newer journal lineage fails
   * closed as `journal_schema_unsupported` (a migration problem, never
   * conflated with a non-journal image), while bytes that are not a journal
   * image at all fail as `journal_image_invalid` — in both cases without
   * executing any of the image's statements.
   */
  static openFromImage(engineModule, image) {
    let engine = null;
    try {
      engine = new engineModule.Database(image);
      const schemaVersion = _SqliteDatabase.#readSchemaVersionOf(engine);
      if (schemaVersion !== JOURNAL_SCHEMA_VERSION) {
        throw journalStoreError("journal_schema_unsupported");
      }
      const database = new _SqliteDatabase(engine);
      database.readJournalMeta();
      return database;
    } catch (error) {
      engine?.close();
      if (error instanceof JournalStoreError) {
        throw error;
      }
      throw journalStoreError("journal_image_invalid");
    }
  }
  static #readSchemaVersionOf(engine) {
    const result = engine.exec("pragma user_version;");
    const value = result[0]?.values[0]?.[0];
    return typeof value === "number" ? value : Number.NaN;
  }
  /** Run one read-only query and return its full result set. */
  readAll(sql) {
    try {
      return this.#engine.exec(sql);
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }
  /** The persisted schema bookkeeping version of this image. */
  readSchemaVersion() {
    try {
      return _SqliteDatabase.#readSchemaVersionOf(this.#engine);
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }
  /** Read the single journal meta row. */
  readJournalMeta() {
    const result = this.readAll(
      [
        "select schema_version, dirty_generation, last_verified_generation,",
        "is_reconcile_required, recovery_state from journal_meta where singleton_key = 1;"
      ].join(" ")
    );
    const row = result[0]?.values[0];
    if (row === void 0) {
      throw journalStoreError("journal_image_invalid");
    }
    try {
      return parseJournalMetaRow(row);
    } catch {
      throw journalStoreError("journal_image_invalid");
    }
  }
  /** Export the current in-memory state as one portable database image. */
  exportImage() {
    try {
      return this.#engine.export();
    } catch {
      throw journalStoreError("journal_query_failed");
    }
  }
  close() {
    this.#engine.close();
  }
  /**
   * The single serialized writer (spec 6.1): every mutation of this database
   * flows through this queue, one transaction at a time, in submission
   * order. A throwing operation rolls its transaction back completely and
   * never leaks the original failure detail.
   */
  async runSerializedMutation(operation) {
    const execution = this.#mutationTail.then(() => this.#executeInTransaction(operation));
    this.#mutationTail = execution.then(
      () => void 0,
      () => void 0
    );
    return execution;
  }
  async #executeInTransaction(operation) {
    let hasTransactionBegun = false;
    try {
      this.#engine.exec("begin immediate;");
      hasTransactionBegun = true;
      const session = {
        exec: (sql) => {
          this.#engine.exec(sql);
        },
        readRows: (sql) => {
          try {
            return this.#engine.exec(sql);
          } catch {
            throw journalStoreError("journal_query_failed");
          }
        },
        readJournalMeta: () => this.readJournalMeta(),
        writeJournalMeta: (meta) => {
          this.#engine.exec(journalMetaWriteSql(meta));
        }
      };
      const result = await operation(session);
      this.#engine.exec("commit;");
      return result;
    } catch (error) {
      if (hasTransactionBegun) {
        try {
          this.#engine.exec("rollback;");
        } catch {
        }
      }
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_mutation_failed");
    }
  }
};
var DEVICE_SYNC_MIGRATION_DDL = [
  DEVICE_SYNC_STATE_DDL,
  MANIFEST_PAGE_PROGRESS_DDL,
  MANIFEST_ACTION_PROGRESS_DDL,
  REMOTE_APPLY_OPERATIONS_DDL,
  ECHO_MARKERS_DDL,
  DEVICE_SYNC_STATE_SEED_SQL,
  "update journal_meta set schema_version = 7 where singleton_key = 1;",
  "pragma user_version = 7;"
].join("");
function readUserVersionOf(engine) {
  const result = engine.exec("pragma user_version;");
  const value = result[0]?.values[0]?.[0];
  return typeof value === "number" ? value : Number.NaN;
}
function restoreReservationJournalImageLooksValid(engine) {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;"
    );
    if (metaRows[0]?.values[0] === void 0) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;"
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files"
    ];
    for (const name of required) {
      if (!tableNames.includes(name)) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}
function migrateRestoreReservationJournalToDeviceSyncSchema(engineModule, image) {
  let engine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== RESTORE_RESERVATION_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!restoreReservationJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(DEVICE_SYNC_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
      }
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}
var MULTIPART_PROGRESS_MIGRATION_DDL = [
  MULTIPART_UPLOAD_PROGRESS_DDL,
  "update journal_meta set schema_version = 8 where singleton_key = 1;",
  "pragma user_version = 8;"
].join("");
function deviceSyncJournalImageLooksValid(engine) {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;"
    );
    if (metaRows[0]?.values[0] === void 0) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;"
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "remote_apply_operations"
    ];
    for (const name of required) {
      if (!tableNames.includes(name)) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}
function migrateDeviceSyncJournalToMultipartProgressSchema(engineModule, image) {
  let engine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== DEVICE_SYNC_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!deviceSyncJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(MULTIPART_PROGRESS_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
      }
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}
var CONFLICT_LOCAL_REPAIR_MIGRATION_DDL = [
  CONFLICT_LOCAL_REPAIRS_DDL,
  "update journal_meta set schema_version = 9 where singleton_key = 1;",
  "pragma user_version = 9;"
].join("");
function multipartProgressJournalImageLooksValid(engine) {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;"
    );
    if (metaRows[0]?.values[0] === void 0) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;"
    );
    const tableNames = (tables[0]?.values ?? []).map((row) => row[0]);
    const required = [
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "multipart_upload_progress",
      "remote_apply_operations"
    ];
    for (const name of required) {
      if (!tableNames.includes(name)) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}
function migrateMultipartProgressJournalToConflictRepairSchema(engineModule, image) {
  let engine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    const currentVersion = readUserVersionOf(engine);
    if (currentVersion !== MULTIPART_PROGRESS_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!multipartProgressJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(CONFLICT_LOCAL_REPAIR_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
      }
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}
var PENDING_RENAME_INTENT_MIGRATION_DDL = [
  PENDING_RENAME_INTENTS_DDL,
  PENDING_RENAME_INTENT_MISSING_FILE_DEFERRALS_DDL,
  "update journal_meta set schema_version = 10 where singleton_key = 1;",
  "pragma user_version = 10;"
].join("");
function conflictRepairJournalImageLooksValid(engine) {
  try {
    const metaRows = engine.exec(
      "select schema_version from journal_meta where singleton_key = 1;"
    );
    if (metaRows[0]?.values[0]?.[0] !== CONFLICT_REPAIR_SCHEMA_VERSION) {
      return false;
    }
    const tables = engine.exec(
      "select name from sqlite_master where type = 'table' order by name;"
    );
    const tableNames = new Set((tables[0]?.values ?? []).map((row) => String(row[0])));
    const required = [
      "conflict_local_repairs",
      "device_sync_state",
      "echo_markers",
      "journal_attempts",
      "journal_events",
      "journal_meta",
      "lifecycle_event_operands",
      "local_files",
      "manifest_action_progress",
      "manifest_page_progress",
      "multipart_upload_progress",
      "remote_apply_operations"
    ];
    return required.every((name) => tableNames.has(name));
  } catch {
    return false;
  }
}
function migrateConflictRepairJournalToPendingRenameIntentSchema(engineModule, image) {
  let engine;
  try {
    engine = new engineModule.Database(image);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    if (readUserVersionOf(engine) !== CONFLICT_REPAIR_SCHEMA_VERSION) {
      throw journalStoreError("journal_schema_unsupported");
    }
    if (!conflictRepairJournalImageLooksValid(engine)) {
      throw journalStoreError("journal_image_invalid");
    }
    engine.exec("begin immediate;");
    try {
      engine.exec(PENDING_RENAME_INTENT_MIGRATION_DDL);
      engine.exec("commit;");
    } catch (error) {
      try {
        engine.exec("rollback;");
      } catch {
      }
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_mutation_failed");
    }
    return engine.export();
  } catch (error) {
    throw error instanceof JournalStoreError ? error : journalStoreError("journal_image_invalid");
  } finally {
    engine.close();
  }
}

// src/journal/lifecycle-contracts.ts
var CHILD_FIVE_SCHEMA_VERSION = 3;
var LIFECYCLE_JOURNAL_OPERATIONS = [
  "rename",
  "move",
  "delete",
  "restore"
];
function isLifecycleJournalOperation(value) {
  return typeof value === "string" && LIFECYCLE_JOURNAL_OPERATIONS.includes(value);
}
var LIFECYCLE_LOCAL_FILE_STATES = [
  "active",
  "rename_pending",
  "move_pending",
  "delete_pending",
  "restore_pending",
  "tombstoned",
  "restored",
  "reconcile_required"
];
function isLifecycleLocalFileState(value) {
  return typeof value === "string" && LIFECYCLE_LOCAL_FILE_STATES.includes(value);
}
function createLifecycleEventOperands(draft) {
  if (!isLifecycleJournalOperation(draft.operation)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof draft.sourceId !== "string" || typeof draft.expectedVersionId !== "string") {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof draft.policyRevision !== "number" || !Number.isInteger(draft.policyRevision) || draft.policyRevision < 1) {
    throw journalStoreError("journal_mutation_failed");
  }
  const capturedSha = draft.capturedFingerprintSha256 ?? null;
  const capturedSize = draft.capturedFingerprintSizeBytes ?? null;
  const capturedMedia = draft.capturedFingerprintMediaType ?? null;
  if (capturedSha === null !== (capturedSize === null) || capturedSha === null !== (capturedMedia === null)) {
    throw journalStoreError("journal_mutation_failed");
  }
  return {
    operation: draft.operation,
    sourceId: draft.sourceId,
    expectedVersionId: draft.expectedVersionId,
    expectedLocator: draft.expectedLocator ?? null,
    targetLocator: draft.targetLocator ?? null,
    tombstoneId: draft.tombstoneId ?? null,
    policyRevision: draft.policyRevision,
    predecessorEventId: draft.predecessorEventId ?? null,
    capturedFingerprintSha256: capturedSha,
    capturedFingerprintSizeBytes: capturedSize,
    capturedFingerprintMediaType: capturedMedia
  };
}
var LIFECYCLE_MIGRATION_DDL = [
  "alter table local_files add column last_locator text;",
  "alter table local_files add column open_tombstone_id text;",
  "alter table local_files add column lifecycle_state text not null default 'active';",
  "create table if not exists lifecycle_event_operands (",
  "  event_id text primary key references journal_events (event_id),",
  "  source_id text not null,",
  "  expected_version_id text not null,",
  "  expected_locator text,",
  "  target_locator text,",
  "  tombstone_id text,",
  "  policy_revision integer not null check (policy_revision >= 1),",
  "  predecessor_event_id text references journal_events (event_id)",
  ");",
  "create index if not exists lifecycle_operands_predecessor_idx",
  "  on lifecycle_event_operands (predecessor_event_id);",
  `update journal_meta set schema_version = ${CHILD_FIVE_SCHEMA_VERSION} where singleton_key = 1;`,
  `pragma user_version = ${CHILD_FIVE_SCHEMA_VERSION};`
].join("");
var LAST_COMMITTED_MIGRATION_DDL = [
  "alter table local_files add column last_committed_sha256 text;",
  "alter table local_files add column last_committed_size_bytes integer;",
  "alter table local_files add column last_committed_media_type text;",
  "update journal_meta set schema_version = 4 where singleton_key = 1;",
  "pragma user_version = 4;"
].join("");
var SERVER_RECEIPT_MIGRATION_DDL = [
  "alter table lifecycle_event_operands add column server_receipt_tombstone_id text;",
  "update journal_meta set schema_version = 5 where singleton_key = 1;",
  "pragma user_version = 5;"
].join("");
var RESTORE_RESERVATION_MIGRATION_DDL = [
  "alter table local_files add column restore_prior_path text;",
  "update journal_meta set schema_version = 6 where singleton_key = 1;",
  "pragma user_version = 6;"
].join("");

// src/journal/sync-diagnostics-export.ts
var SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT = 5;
var SYNC_DIAGNOSTICS_EXPORT_CONTRACT = "obsidian_sync_diagnostics_export/v1";
function renderJournalStoreDiagnosticsLine(input) {
  if (input.lastJournalFailureReasons.length === 0 && input.generationPublishFailureCount === 0) {
    return "No journal store failures observed.";
  }
  const parts = [];
  if (input.lastJournalFailureReasons.length > 0) {
    parts.push(`Pass failures: ${input.lastJournalFailureReasons.join(", ")}`);
  }
  if (input.generationPublishFailureCount > 0) {
    const reasons = input.lastGenerationPublishFailureReasons.join(", ");
    parts.push(
      `Generation publish failures: ${input.generationPublishFailureCount}` + (reasons.length > 0 ? ` (${reasons})` : "")
    );
  }
  return parts.join("\n");
}
var STOP_REASON_KIND_ORDER = [
  "journal_failure",
  "publish_failure",
  "wire_failure"
];
var COMPOSITION_READ_STOP_REASON_EXCLUDED_TOKENS = /* @__PURE__ */ new Set([
  "status_read_failed",
  "note_status_read_failed",
  "retry_schedule_read_failed",
  "sync_status_read_failed"
]);
function deriveSyncStopReasonTokens(entries) {
  const newestByKind = {};
  for (const entry of entries) {
    if (!STOP_REASON_KIND_ORDER.includes(entry.kind)) {
      continue;
    }
    let closedToken;
    for (const token of entry.tokens) {
      if (typeof token === "string" && !COMPOSITION_READ_STOP_REASON_EXCLUDED_TOKENS.has(token)) {
        closedToken = token;
        break;
      }
    }
    if (closedToken !== void 0) {
      newestByKind[entry.kind] = closedToken;
    }
  }
  return STOP_REASON_KIND_ORDER.map((kind) => newestByKind[kind]).filter(
    (token) => token !== void 0
  );
}
function renderTrailToken(token) {
  return typeof token === "string" ? token : `request_id=${token.requestId}`;
}
function renderTrailEntryLine(entry) {
  const timestampText = new Date(entry.atEpochMs).toISOString();
  const tokenText = entry.tokens.map(renderTrailToken).join(" \xB7 ");
  return tokenText.length > 0 ? `${timestampText} \xB7 ${entry.kind} \xB7 ${tokenText}` : `${timestampText} \xB7 ${entry.kind}`;
}
function renderSyncDiagnosticsTrailSection(input) {
  const lines = [];
  if (input.stopReasonTokens.length > 0) {
    lines.push(`Stop reasons: ${input.stopReasonTokens.join(", ")}`);
  }
  lines.push(
    `Trail entries: ${input.totalEntryCount} \xB7 Append failures: ${input.appendFailureCount}`
  );
  const newestFirstTail = input.entries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT).reverse();
  if (newestFirstTail.length === 0) {
    lines.push("No trail entries recorded yet.");
  } else {
    lines.push(...newestFirstTail.map(renderTrailEntryLine));
  }
  return lines.join("\n");
}
function renderSyncDiagnosticsExportBlock(input) {
  const lines = [SYNC_DIAGNOSTICS_EXPORT_CONTRACT];
  lines.push(`Status: ${input.syncStatusLine ?? "Journal not running on this device"}`);
  for (const guidanceLine of input.syncBlockerGuidance) {
    lines.push(`Blocker: ${guidanceLine}`);
  }
  lines.push("Journal store diagnostics:");
  const diagnosticsLines = renderJournalStoreDiagnosticsLine(
    input.journalStoreDiagnostics
  ).split("\n");
  for (const diagnosticsLine of diagnosticsLines) {
    lines.push(diagnosticsLine.length > 0 ? `  ${diagnosticsLine}` : diagnosticsLine);
  }
  lines.push(`Trail entries: ${input.trailEntryCount}`);
  lines.push(`Trail append failures: ${input.trailAppendFailureCount}`);
  lines.push(`Device sync: ${input.deviceSyncStatusLine ?? "not running on this device"}`);
  const newestFirstTail = input.trailTail.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT).reverse();
  if (newestFirstTail.length === 0) {
    lines.push("Trail tail: none recorded");
  } else {
    lines.push(`Trail tail (last ${newestFirstTail.length}):`);
    lines.push(...newestFirstTail.map(renderTrailEntryLine));
  }
  return lines.join("\n");
}

// src/authentication/settings-tab.ts
var DeviceAuthenticationSettingTab = class extends import_obsidian2.PluginSettingTab {
  #view;
  constructor(app, plugin, view) {
    super(app, plugin);
    this.#view = view;
  }
  display() {
    const containerEl = this.containerEl;
    containerEl.empty();
    const snapshot = this.#view.getSnapshot();
    const controls = resolveAuthenticationControls(snapshot.connectionState, {
      hasPendingGrant: snapshot.hasPendingGrant,
      hasActiveCredential: snapshot.hasActiveCredential
    });
    new import_obsidian2.Setting(containerEl).setName("Connection status").setDesc(renderConnectionStatusDescription(snapshot));
    new import_obsidian2.Setting(containerEl).setName("Sync status").setDesc(syncStatusDescription(snapshot));
    new import_obsidian2.Setting(containerEl).setName("Policy state").setDesc(renderPolicyStateGuidanceLine(snapshot.policyState));
    new import_obsidian2.Setting(containerEl).setName("Lifecycle state").setDesc(lifecycleStateCountsDescription(snapshot));
    new import_obsidian2.Setting(containerEl).setName("Lifecycle blockers").setDesc(lifecycleBlockedReasonCodesDescription(snapshot));
    new import_obsidian2.Setting(containerEl).setName("Journal store diagnostics").setDesc(
      renderJournalStoreDiagnosticsLine({
        lastJournalFailureReasons: snapshot.lastJournalFailureReasons,
        generationPublishFailureCount: snapshot.generationPublishFailureCount,
        lastGenerationPublishFailureReasons: snapshot.lastGenerationPublishFailureReasons
      })
    );
    new import_obsidian2.Setting(containerEl).setName("Sync diagnostics trail").setDesc(
      renderSyncDiagnosticsTrailSection({
        stopReasonTokens: snapshot.syncStopReasonTokens,
        totalEntryCount: snapshot.trailEntryCount,
        appendFailureCount: snapshot.trailAppendFailureCount,
        entries: snapshot.trailTailEntries
      })
    );
    new import_obsidian2.Setting(containerEl).setName("Device sync").setDesc(deviceSyncStatusDescription(snapshot.deviceSyncStatus));
    new import_obsidian2.Setting(containerEl).setName("Sync status by note").setDesc(renderLocalNoteSyncStatusList(snapshot.localNoteSyncStatuses));
    new import_obsidian2.Setting(containerEl).setName("Public workspace origin").setDesc("Public origin that serves the Web Admin and proxies /api/* to the knowledge API").addText(
      (text) => text.setPlaceholder("https://vault.example.com").setValue(snapshot.serverOrigin).onChange((value) => this.#view.setServerOrigin(value.trim()))
    );
    new import_obsidian2.Setting(containerEl).setName("Device name").setDesc("1\u201380 display characters shown on the approval page").addText(
      (text) => text.setPlaceholder("Personal vault").setValue(snapshot.deviceName).onChange((value) => this.#view.setDeviceName(value))
    );
    const actionSetting = new import_obsidian2.Setting(containerEl);
    actionSetting.addButton(
      (button) => button.setButtonText("Login").setDisabled(!controls.canLogin).onClick(() => {
        void this.#runAction(this.#view.login());
      })
    );
    actionSetting.addButton(
      (button) => button.setButtonText("Retry connection").setDisabled(!controls.canRetryConnection).onClick(() => {
        void this.#runAction(this.#view.retryConnection());
      })
    );
    actionSetting.addButton(
      (button) => button.setButtonText("Open browser again").setDisabled(!controls.canOpenBrowser).onClick(() => {
        this.#view.openBrowserAgain();
        this.display();
      })
    );
    actionSetting.addButton(
      (button) => button.setButtonText("Cancel pending login").setDisabled(!controls.canCancel).onClick(() => {
        void this.#runAction(this.#view.cancelPendingLogin());
      })
    );
    actionSetting.addButton(
      (button) => button.setButtonText("Disconnect").setDisabled(!controls.canDisconnect).onClick(() => {
        void this.#runAction(this.#view.disconnect());
      })
    );
  }
  #runAction(action) {
    action.then(
      () => this.display(),
      () => this.display()
    );
  }
};
function deviceSyncStatusDescription(status) {
  return status === null ? "Device sync is not running on this device" : renderDeviceSyncStatusText(status);
}
function syncStatusDescription(snapshot) {
  const lines = [];
  if (snapshot.syncStatusText !== null) {
    lines.push(snapshot.syncStatusText);
  }
  lines.push(...snapshot.syncBlockerGuidance);
  const startupFailureLine = renderJournalStartupFailureLine(
    snapshot.lastStartupFailureTokens
  );
  if (startupFailureLine !== null) {
    lines.push(startupFailureLine);
  }
  if (lines.length === 0) {
    return "Journal not running on this device";
  }
  return lines.join(" ");
}
var TERMINAL_CONNECTION_STATES = ["revoked", "not_connected"];
function renderConnectionStatusDescription(snapshot) {
  const statusText = CONNECTION_STATUS_TEXT[snapshot.connectionState];
  const parts = [];
  if (snapshot.statusDetail !== null) {
    parts.push(snapshot.statusDetail);
  }
  const isTerminalConnectionState = TERMINAL_CONNECTION_STATES.includes(
    snapshot.connectionState
  );
  if (isTerminalConnectionState && snapshot.clearedReason !== null && snapshot.clearedReason !== snapshot.statusDetail) {
    parts.push(`Last cleared reason: ${snapshot.clearedReason}`);
  }
  return parts.length === 0 ? statusText : `${statusText} \u2014 ${parts.join(" \xB7 ")}`;
}
var POLICY_STATE_GUIDANCE_TEXT = {
  policy_not_initialized: "Policy not initialized: complete the browser login to establish policy trust before any capture runs.",
  policy_ready: "Policy verified: capture and sync run under the currently accepted policy revision.",
  policy_refresh_required: "Policy refresh required: the accepted policy revision is stale; the next successful credential refresh renews it.",
  policy_offline_cached: "Policy offline cache in use: capture continues under the last verified policy revision until connectivity returns.",
  policy_integrity_failed: "Policy integrity failed: capture is stopped until policy trust is re-established through the authorized login flow."
};
function renderPolicyStateGuidanceLine(policyState) {
  return POLICY_STATE_GUIDANCE_TEXT[policyState];
}
function renderJournalStartupFailureLine(startupFailureTokens) {
  if (startupFailureTokens === null || startupFailureTokens.length === 0) {
    return null;
  }
  return `Journal startup failed: ${startupFailureTokens.join(", ")}`;
}
function lifecycleStateCountsDescription(snapshot) {
  const counts = snapshot.lifecycleStateCounts;
  if (counts === null) {
    return "Journal not running on this device";
  }
  const parts = [];
  for (const state of LIFECYCLE_LOCAL_FILE_STATES) {
    const value = counts[state];
    parts.push(`${LIFECYCLE_STATE_LABEL[state]}: ${value}`);
  }
  parts.push(`Pending lifecycle events: ${snapshot.pendingLifecycleEventCount}`);
  parts.push(`Failed attempts: ${snapshot.failedAttemptCount}`);
  return parts.join(" \xB7 ");
}
function lifecycleBlockedReasonCodesDescription(snapshot) {
  const counts = snapshot.lifecycleStateCounts;
  if (counts === null) {
    return "Journal not running on this device";
  }
  if (snapshot.lifecycleBlockedReasonCodes.length === 0) {
    return "No lifecycle blockers observed";
  }
  return [...snapshot.lifecycleBlockedReasonCodes].join(", ");
}
function renderLocalNoteSyncStatusList(statuses) {
  if (statuses.length === 0) {
    return "No note sync statuses are available on this device";
  }
  return [...statuses].sort(compareNormalizedPathsByCodeUnit).map(renderLocalNoteSyncStatus).join("\n");
}
function compareNormalizedPathsByCodeUnit(left, right) {
  if (left.normalizedPath < right.normalizedPath) {
    return -1;
  }
  if (left.normalizedPath > right.normalizedPath) {
    return 1;
  }
  return 0;
}
function renderLocalNoteSyncStatus(status) {
  const line = `${status.normalizedPath} \u2014 ${LOCAL_NOTE_SYNC_STATE_LABEL[status.state]}`;
  if (status.state === "retrying") {
    return `${line} \xB7 Retry at: ${status.retryAtEpochMs ?? "unavailable"}${renderClosedReason(status.reason)}`;
  }
  if (status.state === "policy_blocked") {
    return `${line} \xB7 Policy revision: ${status.policyRevisionNumber ?? "unknown"}${renderClosedReason(status.reason)}`;
  }
  return line;
}
function renderClosedReason(reason) {
  return reason === null ? "" : ` \xB7 Reason: ${reason}`;
}
var LIFECYCLE_STATE_LABEL = {
  active: "Active",
  rename_pending: "Rename pending",
  move_pending: "Move pending",
  delete_pending: "Delete pending",
  restore_pending: "Restore pending",
  tombstoned: "Tombstoned",
  restored: "Restored",
  reconcile_required: "Reconcile required"
};
var LOCAL_NOTE_SYNC_STATE_LABEL = {
  synced: "Synced",
  queued: "Queued",
  syncing: "Syncing",
  retrying: "Retrying",
  policy_blocked: "Policy blocked",
  conflict: "Conflict",
  reconcile_required: "Reconciliation required"
};

// src/authentication/token-session.ts
function resolveStartupAction(record) {
  if (record?.state === "pending_grant") {
    return "resume_pending_grant";
  }
  if (record?.state === "active") {
    return "refresh_credential";
  }
  return "none";
}
var DeviceTokenSession = class {
  #deps;
  #accessCredential = null;
  #refreshInFlight = null;
  constructor(deps) {
    this.#deps = deps;
  }
  /** The memory-only access credential (never persisted, spec 13.3). */
  get accessCredential() {
    return this.#accessCredential;
  }
  /** Adopt the exchange of a completed onboarding as the live session. */
  adoptExchange(exchange) {
    this.#accessCredential = exchange.access_credential;
  }
  /** Clear the in-memory access credential (plugin unload). */
  clearMemoryAccess() {
    this.#accessCredential = null;
  }
  /**
   * Rotate the refresh credential once (spec 13.3, 13.4): persist the pending
   * rotation identity and verify the readback before the network call, then
   * persist the complete successor in one verified write. A stored pending
   * identity from a crashed attempt is reused so the server replays the exact
   * successor instead of detecting reuse.
   */
  async refresh() {
    if (this.#refreshInFlight !== null) {
      return this.#refreshInFlight;
    }
    const attempt = this.#rotateOnce();
    this.#refreshInFlight = attempt;
    try {
      await attempt;
    } finally {
      this.#refreshInFlight = null;
    }
  }
  async #rotateOnce() {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state !== "active") {
      throw new DeviceAuthError("device_credential_invalid", {
        status: 0,
        message: "no active device credential record is available",
        isLocal: true
      });
    }
    const rotationId = record.pending_rotation_id ?? this.#deps.createRotationId();
    writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
      refresh_credential: record.refresh_credential,
      refresh_generation: record.refresh_generation,
      pending_rotation_id: rotationId
    });
    try {
      const successor = await this.#deps.transport.refresh(record.refresh_credential, rotationId);
      writeActiveDeviceRecord(this.#deps.secretStore, this.#deps.recordName, {
        refresh_credential: successor.refresh_credential,
        refresh_generation: successor.refresh_generation,
        pending_rotation_id: null
      });
      this.#accessCredential = successor.access_credential;
      this.#deps.onStateChange("connected", null);
    } catch (error) {
      await this.#surfaceRefreshFailure(error);
      throw error;
    }
  }
  async #surfaceRefreshFailure(error) {
    const code = isDeviceAuthError(error) ? error.code : null;
    if (code === "device_token_reuse_detected") {
      await this.#clearTerminalRecord("token_reuse", "revoked");
      return;
    }
    if (code === "device_revoked") {
      await this.#clearTerminalRecord("device_revoked", "revoked");
      return;
    }
    if (code === "device_credential_invalid") {
      await this.#clearTerminalRecord("credential_invalid", "revoked");
      return;
    }
    if (code === "network_unavailable") {
      this.#deps.onStateChange("offline", "network_unavailable");
      return;
    }
    this.#deps.onStateChange("refresh_required", resolveDeviceAuthClosedCode(error));
  }
  async #clearTerminalRecord(clearedReason, nextState) {
    writeClearedTombstone(this.#deps.secretStore, this.#deps.recordName, clearedReason);
    this.#deps.settings.secret_record_name = null;
    this.#accessCredential = null;
    await this.#deps.persistSettings();
    this.#deps.onStateChange(nextState, clearedReason);
  }
  /**
   * Self-disconnect (spec 14.2): the server revoke happens FIRST. A confirmed
   * response or a terminal credential response replaces the record with the
   * verified tombstone and clears the settings reference and the in-memory
   * access credential. Transient failures keep the local record for retry.
   */
  async disconnect() {
    const record = readDeviceSecretRecord(this.#deps.secretStore, this.#deps.recordName);
    if (record?.state !== "active") {
      return;
    }
    try {
      await this.#deps.transport.revokeCurrent(record.refresh_credential);
    } catch (error) {
      const code = isDeviceAuthError(error) ? error.code : null;
      if (code === "device_credential_invalid" || code === "device_revoked" || code === "device_token_reuse_detected") {
        await this.#clearTerminalRecord("self_disconnect", "not_connected");
        return;
      }
      if (code === "network_unavailable") {
        this.#deps.onStateChange("offline", "network_unavailable");
      }
      throw error;
    }
    await this.#clearTerminalRecord("self_disconnect", "not_connected");
  }
};

// src/exclusion-policy/canonical-json.ts
var MAXIMUM_SAFE_INTEGER = 9007199254740991;
var MINIMUM_SAFE_INTEGER = -9007199254740991;
var CONTROL_ESCAPES = {
  8: "\\b",
  9: "\\t",
  10: "\\n",
  12: "\\f",
  13: "\\r"
};
function hasUnpairedSurrogate(value) {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 55296 && codeUnit <= 56319) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 56320 && next <= 57343) {
        index += 1;
        continue;
      }
      return true;
    }
    if (codeUnit >= 56320 && codeUnit <= 57343) {
      return true;
    }
  }
  return false;
}
function validateString(value) {
  if (hasUnpairedSurrogate(value)) {
    throw policyVerificationError("policy_value_unsupported");
  }
  if (value.normalize("NFC") !== value) {
    throw policyVerificationError("policy_value_unsupported");
  }
}
function encodeString(value) {
  let pieces = '"';
  for (const character of value) {
    if (character === '"' || character === "\\") {
      pieces += `\\${character}`;
      continue;
    }
    const codePoint = character.codePointAt(0);
    if (codePoint !== void 0 && codePoint < 32) {
      pieces += CONTROL_ESCAPES[codePoint] ?? `\\u${codePoint.toString(16).padStart(4, "0")}`;
      continue;
    }
    pieces += character;
  }
  return `${pieces}"`;
}
function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function encodeInto(value, pieces) {
  if (value === null) {
    pieces.push("null");
    return;
  }
  if (value === true) {
    pieces.push("true");
    return;
  }
  if (value === false) {
    pieces.push("false");
    return;
  }
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value > MAXIMUM_SAFE_INTEGER || value < MINIMUM_SAFE_INTEGER) {
      throw policyVerificationError("policy_value_unsupported");
    }
    if (Object.is(value, -0)) {
      pieces.push("0");
      return;
    }
    pieces.push(value.toString(10));
    return;
  }
  if (typeof value === "string") {
    validateString(value);
    pieces.push(encodeString(value));
    return;
  }
  if (Array.isArray(value)) {
    pieces.push("[");
    for (let index = 0; index < value.length; index += 1) {
      if (index > 0) {
        pieces.push(",");
      }
      encodeInto(value[index], pieces);
    }
    pieces.push("]");
    return;
  }
  if (isPlainObject(value)) {
    const names = Object.keys(value);
    const seen = /* @__PURE__ */ new Set();
    for (const name of names) {
      if (seen.has(name)) {
        throw policyVerificationError("policy_value_unsupported");
      }
      seen.add(name);
    }
    const ordered = [...names].sort((left, right) => left < right ? -1 : left > right ? 1 : 0);
    pieces.push("{");
    for (let position = 0; position < ordered.length; position += 1) {
      if (position > 0) {
        pieces.push(",");
      }
      const name = ordered[position];
      if (name === void 0) {
        throw policyVerificationError("policy_value_unsupported");
      }
      validateString(name);
      pieces.push(encodeString(name));
      pieces.push(":");
      encodeInto(value[name], pieces);
    }
    pieces.push("}");
    return;
  }
  throw policyVerificationError("policy_value_unsupported");
}
function canonicalJsonBytes(value) {
  const pieces = [];
  encodeInto(value, pieces);
  return new TextEncoder().encode(pieces.join(""));
}
function canonicalizeClosedJson(value) {
  return new TextDecoder().decode(canonicalJsonBytes(value));
}
async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const raw = new Uint8Array(digest);
  let hexadecimal = "";
  for (const byte of raw) {
    hexadecimal += byte.toString(16).padStart(2, "0");
  }
  return hexadecimal;
}

// src/exclusion-policy/evaluator.ts
var PolicyRuleError = class extends Error {
  reason;
  constructor(reason) {
    super(`exclusion policy rule contract failed: ${reason}`);
    this.name = "PolicyRuleError";
    this.reason = reason;
  }
};
var LOCATOR_MAXIMUM_BYTES = 4096;
var LOCATOR_MAXIMUM_SEGMENTS = 256;
var LOCATOR_SEGMENT_MAXIMUM_BYTES = 255;
var GLOB_MAXIMUM_BYTES = 1024;
var GLOB_MAXIMUM_SEGMENTS = 64;
var GLOB_MAXIMUM_WILDCARD_TOKENS = 16;
var EXTENSION_MINIMUM_CHARACTERS = 2;
var EXTENSION_MAXIMUM_CHARACTERS = 64;
var MAXIMUM_SIZE_BYTES_CEILING = 104857600;
var MAXIMUM_RULES_PER_EVALUATION = 256;
var RULE_FINGERPRINT_CONTRACT = "exclusion_policy_rule/v1";
var NIL_UUID = "00000000-0000-0000-0000-000000000000";
var UUID_PATTERN2 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var EXTENSION_CHARACTERS = new Set("abcdefghijklmnopqrstuvwxyz0123456789._-");
var MIME_TSPECIALS = /* @__PURE__ */ new Set(["(", ")", "<", ">", "@", ",", ";", ":", "\\", '"', "[", "]", "?", "=", "/", "*"]);
var GLOB_FORBIDDEN_CHARACTERS = /* @__PURE__ */ new Set(["?", "[", "]", "{", "}"]);
function utf8ByteLength(value) {
  return new TextEncoder().encode(value).length;
}
function isControlCharacter(character) {
  const codePoint = character.codePointAt(0) ?? 0;
  return codePoint < 32 || codePoint >= 127 && codePoint <= 159;
}
function nfcOrReject(value) {
  const normalized = value.normalize("NFC");
  if (hasUnpairedSurrogate2(value) || hasUnpairedSurrogate2(normalized)) {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  return normalized;
}
function hasUnpairedSurrogate2(value) {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 55296 && codeUnit <= 56319) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 56320 && next <= 57343) {
        index += 1;
        continue;
      }
      return true;
    }
    if (codeUnit >= 56320 && codeUnit <= 57343) {
      return true;
    }
  }
  return false;
}
function foldAsciiLowercase(value) {
  let folded = "";
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    folded += codeUnit >= 65 && codeUnit <= 90 ? String.fromCharCode(codeUnit + 32) : character;
  }
  return folded;
}
function normalizePolicyLocator(value) {
  if (typeof value !== "string") {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  const normalized = nfcOrReject(value);
  if (normalized.length === 0) {
    throw new PolicyRuleError("locator_empty");
  }
  if (normalized.includes("\\")) {
    throw new PolicyRuleError("locator_backslash_separator");
  }
  if (normalized.startsWith("/")) {
    throw new PolicyRuleError("locator_absolute");
  }
  if (normalized.endsWith("/")) {
    throw new PolicyRuleError("locator_trailing_separator");
  }
  for (const character of normalized) {
    if (isControlCharacter(character)) {
      throw new PolicyRuleError("locator_control_character");
    }
  }
  const segments = normalized.split("/");
  if (segments[0]?.includes(":")) {
    throw new PolicyRuleError("locator_scheme_or_drive");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw new PolicyRuleError("locator_invalid_segment");
    }
  }
  if (segments.length > LOCATOR_MAXIMUM_SEGMENTS) {
    throw new PolicyRuleError("locator_too_many_segments");
  }
  if (utf8ByteLength(normalized) > LOCATOR_MAXIMUM_BYTES) {
    throw new PolicyRuleError("locator_too_long");
  }
  for (const segment of segments) {
    if (utf8ByteLength(segment) > LOCATOR_SEGMENT_MAXIMUM_BYTES) {
      throw new PolicyRuleError("locator_segment_too_long");
    }
  }
  return normalized;
}
function normalizeGlobText(pattern) {
  if (typeof pattern !== "string") {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  const normalized = nfcOrReject(pattern);
  if (normalized.length === 0) {
    throw new PolicyRuleError("locator_empty");
  }
  for (const character of normalized) {
    if (GLOB_FORBIDDEN_CHARACTERS.has(character)) {
      throw new PolicyRuleError("glob_unsupported_token");
    }
  }
  if (normalized.includes("\\")) {
    throw new PolicyRuleError("locator_backslash_separator");
  }
  if (normalized.startsWith("/")) {
    throw new PolicyRuleError("locator_absolute");
  }
  if (normalized.endsWith("/")) {
    throw new PolicyRuleError("locator_trailing_separator");
  }
  const segments = normalized.split("/");
  for (const segment of segments) {
    if (segment.startsWith("!")) {
      throw new PolicyRuleError("glob_unsupported_token");
    }
  }
  for (const character of normalized) {
    if (isControlCharacter(character)) {
      throw new PolicyRuleError("locator_control_character");
    }
  }
  if (segments[0]?.includes(":")) {
    throw new PolicyRuleError("locator_scheme_or_drive");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw new PolicyRuleError("locator_invalid_segment");
    }
  }
  if (segments.length > GLOB_MAXIMUM_SEGMENTS) {
    throw new PolicyRuleError("glob_too_many_segments");
  }
  if (utf8ByteLength(normalized) > GLOB_MAXIMUM_BYTES) {
    throw new PolicyRuleError("glob_too_long");
  }
  let wildcardCount = 0;
  for (const character of normalized) {
    if (character === "*") {
      wildcardCount += 1;
    }
  }
  if (wildcardCount > GLOB_MAXIMUM_WILDCARD_TOKENS) {
    throw new PolicyRuleError("glob_too_many_wildcards");
  }
  return normalized;
}
function compileSegment(segment) {
  if (segment === "**") {
    return { isDoubleStar: true, parts: [] };
  }
  const parts = [];
  let literal = "";
  for (const character of segment) {
    if (character === "*") {
      if (literal.length > 0) {
        parts.push({ kind: "literal", text: literal });
        literal = "";
      }
      parts.push({ kind: "star", text: "" });
      continue;
    }
    literal += character;
  }
  if (literal.length > 0) {
    parts.push({ kind: "literal", text: literal });
  }
  return { isDoubleStar: false, parts };
}
function compilePolicyGlob(pattern) {
  const normalized = normalizeGlobText(pattern);
  return { segments: normalized.split("/").map((segment) => compileSegment(segment)) };
}
function segmentMatches(parts, value) {
  const partCount = parts.length;
  const valueLength = value.length;
  let partIndex = 0;
  let valueIndex = 0;
  let backtrackPart = -1;
  let backtrackValue = 0;
  while (valueIndex < valueLength) {
    if (partIndex < partCount) {
      const part = parts[partIndex];
      if (part !== void 0 && part.kind === "literal") {
        if (value.startsWith(part.text, valueIndex)) {
          valueIndex += part.text.length;
          partIndex += 1;
          continue;
        }
      } else {
        backtrackPart = partIndex;
        backtrackValue = valueIndex;
        partIndex += 1;
        continue;
      }
    }
    if (backtrackPart >= 0) {
      backtrackValue += 1;
      valueIndex = backtrackValue;
      partIndex = backtrackPart + 1;
      continue;
    }
    return false;
  }
  while (partIndex < partCount) {
    if (parts[partIndex]?.kind !== "star") {
      return false;
    }
    partIndex += 1;
  }
  return true;
}
function globMatches(compiled, locatorSegments) {
  const pathCount = locatorSegments.length;
  let reachable = new Array(pathCount + 1).fill(false);
  reachable[0] = true;
  for (const segment of compiled.segments) {
    const nextReachable = new Array(pathCount + 1).fill(false);
    if (segment.isDoubleStar) {
      let prefixReachable = false;
      for (let index = 0; index <= pathCount; index += 1) {
        prefixReachable = prefixReachable || (reachable[index] ?? false);
        nextReachable[index] = prefixReachable;
      }
    } else {
      for (let index = 0; index < pathCount; index += 1) {
        if (reachable[index] && segmentMatches(segment.parts, locatorSegments[index] ?? "")) {
          nextReachable[index + 1] = true;
        }
      }
    }
    reachable = nextReachable;
  }
  return reachable[pathCount] ?? false;
}
function isFamilyTypeToken(value) {
  if (value.length === 0) {
    return false;
  }
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 33 || codeUnit > 126) {
      return false;
    }
    if (MIME_TSPECIALS.has(character)) {
      return false;
    }
    if (codeUnit >= 65 && codeUnit <= 90) {
      return false;
    }
  }
  return true;
}
function isCanonicalMediaType(value) {
  const separatorIndex = value.indexOf("/");
  if (separatorIndex <= 0 || separatorIndex !== value.lastIndexOf("/")) {
    return false;
  }
  const typePart = value.slice(0, separatorIndex);
  const subtypePart = value.slice(separatorIndex + 1);
  if (typePart.length === 0 || subtypePart.length === 0) {
    return false;
  }
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit === 47) {
      continue;
    }
    if (codeUnit < 33 || codeUnit > 126) {
      return false;
    }
    if (MIME_TSPECIALS.has(character)) {
      return false;
    }
    if (codeUnit >= 65 && codeUnit <= 90) {
      return false;
    }
    if (character === "*") {
      return false;
    }
  }
  return true;
}
function normalizeExtensionOperand(textOperand) {
  const folded = foldAsciiLowercase(textOperand);
  if (folded.length < EXTENSION_MINIMUM_CHARACTERS || folded.length > EXTENSION_MAXIMUM_CHARACTERS) {
    throw new PolicyRuleError("operand_invalid");
  }
  if (!folded.startsWith(".")) {
    throw new PolicyRuleError("operand_invalid");
  }
  for (const character of folded) {
    if (!EXTENSION_CHARACTERS.has(character)) {
      throw new PolicyRuleError("operand_invalid");
    }
  }
  return folded;
}
function requireUuid(value, reason) {
  if (!UUID_PATTERN2.test(value) || value === NIL_UUID) {
    throw new PolicyRuleError(reason);
  }
  return value;
}
function fingerprintEnvelopeValue(ruleKind, operand) {
  const envelope = {
    contract: RULE_FINGERPRINT_CONTRACT,
    rule_kind: ruleKind
  };
  switch (operand.kind) {
    case "exact_source_id":
      envelope["source_id"] = operand.sourceId;
      break;
    case "folder_prefix":
      envelope["folder_prefix"] = operand.folderPrefix;
      break;
    case "path_glob":
      envelope["path_glob"] = operand.pattern;
      break;
    case "extension":
      envelope["extension"] = operand.extension;
      break;
    case "media_type":
      envelope["media_type"] = operand.exact ?? `${operand.familyType ?? ""}/*`;
      break;
    case "maximum_size":
      envelope["maximum_size_bytes"] = operand.maximumSizeBytes;
      break;
    case "source_type":
      envelope["source_type"] = operand.sourceType;
      break;
  }
  return envelope;
}
async function normalizePolicyRule(input) {
  requireUuid(input.ruleId, "rule_id_invalid");
  const populated = [input.sourceIdOperand ?? null, input.textOperand ?? null, input.sizeBytesOperand ?? null].filter(
    (operand2) => operand2 !== null
  );
  if (populated.length === 0) {
    throw new PolicyRuleError("operand_missing");
  }
  if (populated.length > 1) {
    throw new PolicyRuleError("operand_conflict");
  }
  let operand;
  switch (input.ruleKind) {
    case "exact_source_id":
      if (typeof input.sourceIdOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = {
        kind: "exact_source_id",
        sourceId: requireUuid(input.sourceIdOperand, "operand_invalid")
      };
      break;
    case "folder_prefix":
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "folder_prefix", folderPrefix: normalizePolicyLocator(input.textOperand) };
      break;
    case "path_glob": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      const pattern = normalizeGlobText(input.textOperand);
      operand = { kind: "path_glob", pattern, compiled: compilePolicyGlob(pattern) };
      break;
    }
    case "extension":
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "extension", extension: normalizeExtensionOperand(input.textOperand) };
      break;
    case "media_type": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      const separatorIndex = input.textOperand.indexOf("/");
      if (separatorIndex > 0 && input.textOperand.length === separatorIndex + 2 && input.textOperand.endsWith("/*")) {
        const familyType = input.textOperand.slice(0, separatorIndex);
        if (!isFamilyTypeToken(familyType)) {
          throw new PolicyRuleError("operand_invalid");
        }
        operand = { kind: "media_type", exact: null, familyType };
        break;
      }
      if (!isCanonicalMediaType(input.textOperand)) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "media_type", exact: input.textOperand, familyType: null };
      break;
    }
    case "maximum_size": {
      const sizeBytes = input.sizeBytesOperand;
      if (typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 0 || sizeBytes > MAXIMUM_SIZE_BYTES_CEILING) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "maximum_size", maximumSizeBytes: sizeBytes };
      break;
    }
    case "source_type": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      if (!SOURCE_TYPES.includes(input.textOperand)) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "source_type", sourceType: input.textOperand };
      break;
    }
  }
  const fingerprint = await sha256Hex(
    canonicalJsonBytes(fingerprintEnvelopeValue(input.ruleKind, operand))
  );
  return {
    ruleId: input.ruleId,
    ruleKind: input.ruleKind,
    operand,
    semanticFingerprint: fingerprint
  };
}
var REQUIRED_FIELD_BY_KIND = {
  exact_source_id: "source_id",
  folder_prefix: "normalized_locator",
  path_glob: "normalized_locator",
  extension: "normalized_locator",
  media_type: "media_type",
  maximum_size: "size_bytes",
  source_type: "source_type"
};
function ruleMatches(rule, subject) {
  const operand = rule.operand;
  switch (operand.kind) {
    case "exact_source_id":
      if (subject.sourceId == null) {
        return null;
      }
      return subject.sourceId === operand.sourceId;
    case "folder_prefix": {
      if (subject.normalizedLocator == null) {
        return null;
      }
      const segments = subject.normalizedLocator.split("/");
      const prefixSegments = operand.folderPrefix.split("/");
      return segments.slice(0, prefixSegments.length).join("/") === operand.folderPrefix;
    }
    case "path_glob":
      if (subject.normalizedLocator == null) {
        return null;
      }
      return globMatches(operand.compiled, subject.normalizedLocator.split("/"));
    case "extension": {
      if (subject.normalizedLocator == null) {
        return null;
      }
      const finalFilename = subject.normalizedLocator.split("/").pop() ?? "";
      return foldAsciiLowercase(finalFilename).endsWith(operand.extension);
    }
    case "media_type": {
      if (subject.mediaType == null) {
        return null;
      }
      if (operand.exact !== null) {
        return subject.mediaType === operand.exact;
      }
      return subject.mediaType.split("/")[0] === operand.familyType;
    }
    case "maximum_size":
      if (subject.sizeBytes == null) {
        return null;
      }
      return subject.sizeBytes > operand.maximumSizeBytes;
    case "source_type":
      if (subject.sourceType == null) {
        return null;
      }
      return subject.sourceType === operand.sourceType;
  }
}
function validateSubject(subject, workspaceId) {
  if (subject.workspaceId !== workspaceId) {
    throw new PolicyRuleError("subject_workspace_mismatch");
  }
  if (subject.sourceId != null) {
    requireUuid(subject.sourceId, "subject_id_invalid");
  }
  if (subject.normalizedLocator != null) {
    if (typeof subject.normalizedLocator !== "string") {
      throw new PolicyRuleError("subject_field_type_invalid");
    }
    if (normalizePolicyLocator(subject.normalizedLocator) !== subject.normalizedLocator) {
      throw new PolicyRuleError("subject_locator_not_normalized");
    }
  }
  if (subject.sourceType != null && !SOURCE_TYPES.includes(subject.sourceType)) {
    throw new PolicyRuleError("subject_field_type_invalid");
  }
  if (subject.mediaType != null && !isCanonicalMediaType(subject.mediaType)) {
    throw new PolicyRuleError("subject_field_type_invalid");
  }
  if (subject.sizeBytes != null) {
    if (typeof subject.sizeBytes !== "number" || !Number.isInteger(subject.sizeBytes) || subject.sizeBytes < 0) {
      throw new PolicyRuleError("subject_size_invalid");
    }
  }
}
function evaluatePolicy(rules, subject, options) {
  if (rules.length > MAXIMUM_RULES_PER_EVALUATION) {
    throw new PolicyRuleError("rule_count_invalid");
  }
  validateSubject(subject, options.workspaceId);
  const matchedRuleIds = [];
  const missingFields = /* @__PURE__ */ new Set();
  for (const rule of rules) {
    const outcome = ruleMatches(rule, subject);
    if (outcome === null) {
      missingFields.add(REQUIRED_FIELD_BY_KIND[rule.ruleKind]);
    } else if (outcome) {
      matchedRuleIds.push(rule.ruleId);
    }
  }
  matchedRuleIds.sort();
  const sortedMissingFields = [...missingFields].sort();
  if (matchedRuleIds.length > 0) {
    return {
      raw: "excluded",
      enforced: "excluded",
      matchedRuleIds,
      missingFields: sortedMissingFields
    };
  }
  if (missingFields.size > 0) {
    return {
      raw: "indeterminate",
      enforced: "excluded",
      matchedRuleIds,
      missingFields: sortedMissingFields
    };
  }
  return { raw: "allowed", enforced: "allowed", matchedRuleIds, missingFields: sortedMissingFields };
}

// src/journal/fingerprint.ts
var FALLBACK_MEDIA_TYPE = "application/octet-stream";
var FROZEN_FINGERPRINT_SHA256_PATTERN = /^[0-9a-f]{64}$/;
function hasAsciiPrefix(content, offset, literal) {
  if (content.byteLength < offset + literal.length) {
    return false;
  }
  for (let index = 0; index < literal.length; index += 1) {
    if (content[offset + index] !== literal.charCodeAt(index)) {
      return false;
    }
  }
  return true;
}
function isStrictUtf8(content) {
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(content);
    return true;
  } catch {
    return false;
  }
}
function sniffMediaType(content) {
  if (hasAsciiPrefix(content, 0, "\x89PNG\r\n\n")) {
    return "image/png";
  }
  if (hasAsciiPrefix(content, 0, "\xFF\xD8\xFF")) {
    return "image/jpeg";
  }
  if (hasAsciiPrefix(content, 0, "GIF87a") || hasAsciiPrefix(content, 0, "GIF89a")) {
    return "image/gif";
  }
  if (hasAsciiPrefix(content, 0, "RIFF") && hasAsciiPrefix(content, 8, "WEBP")) {
    return "image/webp";
  }
  if (hasAsciiPrefix(content, 0, "%PDF-")) {
    return "application/pdf";
  }
  if (hasAsciiPrefix(content, 4, "ftyp")) {
    return "video/mp4";
  }
  if (isStrictUtf8(content)) {
    return "text/plain";
  }
  return FALLBACK_MEDIA_TYPE;
}
async function deriveFrozenFingerprint(contentBytes) {
  return {
    sha256: await sha256Hex(contentBytes),
    sizeBytes: contentBytes.byteLength,
    mediaType: sniffMediaType(contentBytes)
  };
}
function isFrozenFingerprintShape(value) {
  return typeof value.sha256 === "string" && FROZEN_FINGERPRINT_SHA256_PATTERN.test(value.sha256) && typeof value.sizeBytes === "number" && Number.isInteger(value.sizeBytes) && value.sizeBytes >= 0 && typeof value.mediaType === "string" && isCanonicalMediaType(value.mediaType);
}

// src/journal/capture.ts
var EXISTING_FILES_SCAN_MAXIMUM_FILES = MAX_PENDING_EVENTS;
var EXISTING_FILES_SCAN_BATCH_FILES = 100;
var PENDING_EVENT_STATES = new Set(JOURNAL_PENDING_EVENT_STATES);
function isPendingEventState(state) {
  return PENDING_EVENT_STATES.has(state);
}
function isPositiveInteger(value) {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}
function cancelledSummary() {
  return {
    outcome: "cancelled",
    processedFileCount: 0,
    skippedFileCount: 0,
    queuedEventCount: 0,
    isTruncated: false
  };
}
function stoppedSummary() {
  return {
    outcome: "stopped",
    processedFileCount: 0,
    skippedFileCount: 0,
    queuedEventCount: 0,
    isTruncated: false
  };
}
function fingerprintsMatch(left, right) {
  return left !== null && left.sha256 === right.sha256 && left.sizeBytes === right.sizeBytes && left.mediaType === right.mediaType;
}
function isStoreError(error) {
  return error !== null && typeof error === "object" && "reason" in error && typeof error.reason === "string";
}
var JournalCapture = class {
  #repository;
  #vaultReader;
  #policyGate;
  #lifecycleCapture;
  #echoSuppressor;
  #scanMaximumFiles;
  #scanBatchFiles;
  #failureReporter;
  #settleTimers = /* @__PURE__ */ new Map();
  #settleWaiters = /* @__PURE__ */ new Map();
  #lifecycleGuardedPaths = /* @__PURE__ */ new Set();
  #admissionTail = Promise.resolve();
  #isDisposed = false;
  constructor(options) {
    if (options.scanMaximumFiles !== void 0 && !isPositiveInteger(options.scanMaximumFiles)) {
      throw new TypeError("invalid scan maximum");
    }
    if (options.scanBatchFiles !== void 0 && !isPositiveInteger(options.scanBatchFiles)) {
      throw new TypeError("invalid scan batch size");
    }
    this.#repository = options.repository;
    this.#vaultReader = options.vaultReader;
    this.#policyGate = options.policyGate;
    this.#lifecycleCapture = options.lifecycleCapture;
    this.#echoSuppressor = options.echoSuppressor ?? null;
    this.#scanMaximumFiles = options.scanMaximumFiles ?? EXISTING_FILES_SCAN_MAXIMUM_FILES;
    this.#scanBatchFiles = options.scanBatchFiles ?? EXISTING_FILES_SCAN_BATCH_FILES;
    this.#failureReporter = options.failureReporter ?? null;
  }
  /**
   * Queue one create/modify observation (spec 7.1): the path settles alone
   * for the frozen delay — a later observation restarts that one timer —
   * and only the settled read is admitted. Paths currently deferred by a
   * lifecycle guard are refused until the owning rename/move commits
   * server-side and releases the guard (fix round 2 D7). The returned
   * promise resolves once this observation's settled admission is durable
   * (superseded observations of the same path resolve with the one shared
   * admission), or immediately when nothing will be admitted — the hook a
   * Vault-event listener uses to trigger the following queue pass after
   * the event it caused exists in the journal. Never rejects.
   */
  notifyPathChanged(path) {
    if (this.#isDisposed) {
      return Promise.resolve();
    }
    const normalizedPath = this.#normalizePathOrNull(path);
    if (normalizedPath === null || this.#isLifecycleDeferredPath(normalizedPath)) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const waiters = this.#settleWaiters.get(normalizedPath) ?? /* @__PURE__ */ new Set();
      waiters.add(resolve);
      this.#settleWaiters.set(normalizedPath, waiters);
      const runningTimer = this.#settleTimers.get(normalizedPath);
      if (runningTimer !== void 0) {
        clearTimeout(runningTimer);
      }
      this.#settleTimers.set(
        normalizedPath,
        setTimeout(() => {
          this.#settleTimers.delete(normalizedPath);
          this.#admissionTail = this.#admissionTail.then(() => this.#admitNormalizedPath(normalizedPath)).then(
            () => void 0,
            () => {
              this.#failureReporter?.reportJournalFailure("settled_admission_failed");
              return void 0;
            }
          ).then(() => this.#releaseSettleWaiters(normalizedPath));
        }, FILE_SETTLE_DELAY_MS)
      );
    });
  }
  /** Resolve and drop every pending waiter of one settled path. */
  #releaseSettleWaiters(normalizedPath) {
    const waiters = this.#settleWaiters.get(normalizedPath);
    if (waiters === void 0) {
      return;
    }
    this.#settleWaiters.delete(normalizedPath);
    for (const resolve of waiters) {
      resolve();
    }
  }
  /**
   * Resolve once every already-scheduled settle admission has settled — the
   * hook a safe unload uses before closing the journal store. Never rejects.
   */
  whenIdle() {
    return this.#admissionTail;
  }
  /**
   * Observe one delete notification (spec 7.1): the lifecycle capture
   * owns the durable delete event; an untracked path stays untouched and
   * an uncommitted file (no source identity) fails closed after freezing
   * its pending content work. An exact echo marker of our own remote
   * tombstone consumes the observation here — the lifecycle capture is
   * never reached (device cursor child 6, task 10).
   */
  async notifyPathDeleted(file) {
    if (this.#isDisposed) {
      return;
    }
    if (this.#echoSuppressor !== null) {
      const normalizedPath = this.#normalizePathOrNull(file.path);
      if (normalizedPath !== null) {
        const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
        const consumed = await this.#echoSuppressor.consumeDeleteObservation({
          priorLocator: normalizedPath,
          sourceId: trackedFile?.sourceId ?? null
        });
        if (consumed) {
          return;
        }
      }
    }
    try {
      await this.#lifecycleCapture.captureDelete(file);
    } catch (error) {
      if (!isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
  }
  /**
   * Observe one rename notification (spec 7.1): the lifecycle capture
   * owns the durable rename / move event. The per-path settle debounce
   * is applied by the lifecycle capture so a burst collapses into one
   * durable row; a file whose local source identity is missing fails
   * closed with `reconcile_required` durably flagged.
   *
   * After the lifecycle capture settles, the NEW path is always scheduled
   * for one settle admission too: for a committed rename the admission is
   * a fingerprint-matched no-op, and for the uncommitted-transit heal
   * (an unsynced note renamed off the vault's untitled-transit name) it
   * is what re-admits the file fresh under its real name.
   */
  async notifyPathRenamed(file, priorPath) {
    if (this.#isDisposed) {
      return;
    }
    let resolvedLifecycleOwnerLocalFileId = null;
    try {
      await this.#lifecycleCapture.captureRename(file, priorPath, {
        onOwnerResolved(localFileId) {
          resolvedLifecycleOwnerLocalFileId = localFileId;
        }
      });
    } catch (error) {
      if (!isStoreError(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
    const normalizedNewPath = this.#normalizePathOrNull(file.path);
    if (normalizedNewPath === null) {
      return;
    }
    await new Promise((resolve) => {
      this.#admissionTail = this.#admissionTail.then(
        () => this.#admitNormalizedPath(
          normalizedNewPath,
          void 0,
          resolvedLifecycleOwnerLocalFileId
        )
      ).then(
        () => void 0,
        () => {
          this.#failureReporter?.reportJournalFailure("settled_admission_failed");
          return void 0;
        }
      ).then(() => resolve());
    });
  }
  /**
   * The explicit `Sync existing files` pass (spec 7.1): the user confirms
   * first, then one bounded snapshot is processed in bounded batches
   * through the same admission path as settled events. Lifecycle-deferred
   * paths are excluded from the snapshot until their owning rename/move
   * commits server-side (which deletes the marker rows and releases the
   * path for re-admission — fix round 2 D7); a terminally-failed rename
   * keeps the exclusion fail-closed.
   */
  async runExistingFilesScan(options) {
    if (this.#isDisposed) {
      return cancelledSummary();
    }
    if (!await options.confirm()) {
      return cancelledSummary();
    }
    return this.#captureSnapshot();
  }
  /**
   * Reconcile current Vault bytes without user confirmation. This is the
   * automatic coordinator's narrow operation: it preserves every bounded,
   * deterministic admission invariant of the explicit snapshot path.
   */
  async runAutomaticSnapshot(options = {}) {
    if (this.#isSnapshotStopped(options.signal)) {
      return stoppedSummary();
    }
    return this.#captureSnapshot(options.signal, true);
  }
  /**
   * The repair admission of one planned `upload` action (task 11, spec
   * 12.4): runs the SAME settle admission path as the watcher — re-read
   * the settled bytes, gate by the current accepted policy, echo-suppress
   * our own remote apply — and returns the durable capture outcome. An
   * `event_recorded`/`event_coalesced` outcome is the durably created or
   * reauthorized outbound event that terminalizes the action; `null`
   * means nothing is owed (the bytes match the committed proof, the
   * observation was our own echo, or a lifecycle deferral owns the path).
   */
  async admitForRepair(normalizedPath) {
    return this.#admitNormalizedPath(normalizedPath);
  }
  /**
   * The mandatory pre-apply recheck of one planned manifest action (task
   * 11, spec 12.4): the restore reservation, any newer local journal
   * event, the current path/fingerprint and the current accepted policy
   * are all re-proven BEFORE any mutation. An upload's own pending event
   * is the terminalization itself, so only its presence (not a pending
   * event) is rechecked; a vanished or diverged target blocks with its
   * closed action-reason token and no Vault write happens.
   */
  async recheckForRepair(input) {
    const normalizedPath = this.#normalizePathOrNull(input.normalizedLocator);
    if (normalizedPath === null) {
      return { kind: "blocked", reason: "device_manifest_action_stale" };
    }
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile !== null && this.#readLifecycleStateOf(trackedFile.localFileId) === "restore_pending") {
      return { kind: "blocked", reason: "device_manifest_target_occupied" };
    }
    if (input.actionKind !== "upload") {
      const events = trackedFile === null ? [] : this.#repository.readEventsByLocalFileId(trackedFile.localFileId);
      const hasNewerLocalEdit = events.some(
        (event) => isPendingEventState(event.state) && (event.operation === "create" || event.operation === "update")
      );
      if (hasNewerLocalEdit) {
        return { kind: "blocked", reason: "device_manifest_action_stale" };
      }
    }
    const contentBytes = await this.#vaultReader.readRegularFileBytes(normalizedPath);
    if (contentBytes === null) {
      return { kind: "blocked", reason: "device_manifest_action_stale" };
    }
    const currentFingerprint = await deriveFrozenFingerprint(contentBytes);
    if (input.actionKind !== "upload" && !fingerprintsMatch(input.entryFingerprint, currentFingerprint)) {
      return { kind: "blocked", reason: "device_manifest_local_diverged" };
    }
    const evaluation = this.#policyGate.evaluateForCapture({
      sourceId: trackedFile?.sourceId ?? null,
      normalizedLocator: normalizedPath,
      mediaType: currentFingerprint.mediaType,
      sizeBytes: currentFingerprint.sizeBytes
    });
    if (evaluation.decision.enforced !== "allowed") {
      return { kind: "blocked", reason: "device_manifest_policy_excluded" };
    }
    return { kind: "safe" };
  }
  /** Enumerate and admit one deterministic bounded regular-file snapshot. */
  async #captureSnapshot(signal, shouldReportAdmissionFailure = false) {
    if (this.#isSnapshotStopped(signal)) {
      return stoppedSummary();
    }
    const snapshotPaths = await this.#vaultReader.listRegularFilePaths();
    if (this.#isSnapshotStopped(signal)) {
      return stoppedSummary();
    }
    const normalizedSnapshotPaths = [
      ...new Set(snapshotPaths.map((path) => this.#normalizePathOrNull(path)).filter(
        (normalizedPath) => normalizedPath !== null
      ))
    ].sort();
    const boundedPaths = normalizedSnapshotPaths.slice(0, this.#scanMaximumFiles);
    const isTruncated = boundedPaths.length < normalizedSnapshotPaths.length;
    let processedFileCount = 0;
    let skippedFileCount = 0;
    let queuedEventCount = 0;
    let hasReportedAdmissionFailure = false;
    for (let offset = 0; offset < boundedPaths.length; offset += this.#scanBatchFiles) {
      const batchPaths = boundedPaths.slice(offset, offset + this.#scanBatchFiles);
      for (const normalizedPath of batchPaths) {
        if (this.#isSnapshotStopped(signal)) {
          return stoppedSummary();
        }
        if (this.#isLifecycleDeferredPath(normalizedPath)) {
          skippedFileCount += 1;
          continue;
        }
        try {
          const captureResult = await this.#admitNormalizedPath(normalizedPath, signal);
          if (this.#isSnapshotStopped(signal)) {
            return stoppedSummary();
          }
          processedFileCount += 1;
          if (captureResult !== null && (captureResult.outcome === "event_recorded" || captureResult.outcome === "event_coalesced") && captureResult.event.state === "queued") {
            queuedEventCount += 1;
          }
        } catch {
          if (shouldReportAdmissionFailure && !hasReportedAdmissionFailure) {
            this.#failureReporter?.reportJournalFailure("automatic_snapshot_admission_failed");
            hasReportedAdmissionFailure = true;
          }
          skippedFileCount += 1;
        }
      }
    }
    return {
      outcome: "completed",
      processedFileCount,
      skippedFileCount,
      queuedEventCount,
      isTruncated
    };
  }
  /** Stop all settling and release the session guard set (unload/suspend). */
  dispose() {
    this.#isDisposed = true;
    for (const settleTimer of this.#settleTimers.values()) {
      clearTimeout(settleTimer);
    }
    this.#settleTimers.clear();
    for (const normalizedPath of [...this.#settleWaiters.keys()]) {
      this.#releaseSettleWaiters(normalizedPath);
    }
    this.#lifecycleGuardedPaths.clear();
  }
  // --- internals ---------------------------------------------------------------------------------
  /**
   * The one admission path every observation flows through (spec 7.1):
   * re-read the settled bytes, fingerprint exactly those bytes, then gate
   * by the size ceiling and the current accepted policy decision, recording
   * the accepted revision on the journal row.
   *
   * Automatic restore (Child 5 task 8 fix round 1 C2): a tombstoned
   * local mapping that re-appears with bytes matching the last
   * committed fingerprint is restored by the lifecycle capture, not
   * minted as a fresh create. A detection failure (mismatched bytes,
   * missing identity, anything other than a successful restore) is
   * FAIL-CLOSED: the row is durably flagged `reconcile_required`, the
   * open tombstone is cleared, and the create / update admission is
   * refused. The user can still edit the file via the explicit
   * restore surface; the brief disallows the fall-through-to-create
   * behaviour because a successful re-bind of the prior source would
   * silently drop the delete intent.
   */
  async #admitNormalizedPath(normalizedPath, signal, resolvedLifecycleOwnerLocalFileId = null) {
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    if (this.#isLifecycleDeferredPath(normalizedPath)) {
      return null;
    }
    try {
      if (resolvedLifecycleOwnerLocalFileId !== null && this.#repository.lifecycle.readPendingRenameIntentForLocalFile(
        resolvedLifecycleOwnerLocalFileId
      ) !== null) {
        return null;
      }
      if (this.#repository.lifecycle.readPendingRenameIntentByCurrentPath(normalizedPath) !== null) {
        return null;
      }
    } catch (error) {
      this.#failureReporter?.reportJournalFailure("pending_rename_intent_read_failed");
      if (isStoreError(error)) {
        throw error;
      }
      throw journalStoreError("journal_query_failed");
    }
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile !== null && trackedFile.sourceId !== null && trackedFile.baseVersionId !== null) {
      const openTombstone = this.#readOpenTombstoneId(trackedFile.localFileId);
      if (openTombstone !== null) {
        const capture = this.#lifecycleCapture;
        try {
          await capture.detectAutomaticRestore(normalizedPath);
        } catch {
          await capture.markTombstonedPathReconcileRequired(normalizedPath).catch(
            () => void 0
          );
          this.#lifecycleGuardedPaths.add(normalizedPath);
        }
        return null;
      }
    }
    const contentBytes = await this.#vaultReader.readRegularFileBytes(normalizedPath);
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    if (contentBytes === null) {
      if (trackedFile !== null) {
        this.#lifecycleGuardedPaths.add(normalizedPath);
        await this.#deferTrackedFile(trackedFile);
      }
      return null;
    }
    const fingerprint = await deriveFrozenFingerprint(contentBytes);
    if (this.#isSnapshotStopped(signal)) {
      return null;
    }
    if (this.#echoSuppressor !== null) {
      const consumed = await this.#echoSuppressor.consumeContentObservation({
        normalizedLocator: normalizedPath,
        sourceId: trackedFile?.sourceId ?? null,
        fingerprint
      });
      if (consumed) {
        return null;
      }
    }
    const evaluation = this.#policyGate.evaluateForCapture({
      sourceId: trackedFile?.sourceId ?? null,
      normalizedLocator: normalizedPath,
      mediaType: fingerprint.mediaType,
      sizeBytes: fingerprint.sizeBytes
    });
    const admission = fingerprint.sizeBytes > MAX_MULTIPART_FILE_SIZE_BYTES ? "blocked_size" : evaluation.decision.enforced === "allowed" ? "policy_allowed" : "excluded_policy";
    if (admission === "policy_allowed" && fingerprintsMatch(trackedFile?.lastCommittedFingerprint ?? null, fingerprint)) {
      return null;
    }
    return this.#repository.recordCapture({
      normalizedPath,
      fingerprint,
      policyRevisionNumber: evaluation.revisionNumber,
      admission
    });
  }
  #isSnapshotStopped(signal) {
    return this.#isDisposed || signal?.aborted === true;
  }
  /** Defer every still-pending event of one tracked path (spec 7.1). */
  async #deferLifecycleForPath(normalizedPath) {
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile === null) {
      return;
    }
    await this.#deferTrackedFile(trackedFile);
  }
  async #deferTrackedFile(trackedFile) {
    const events = this.#repository.readEventsByLocalFileId(trackedFile.localFileId);
    for (const event of events) {
      if (!isPendingEventState(event.state)) {
        continue;
      }
      const resolution = await this.#repository.resolveIntentAwareLocalFileMissing({
        eventId: event.eventId,
        attemptedAtEpochMs: Date.now(),
        requestCorrelationId: crypto.randomUUID(),
        nextEligibleRetryEpochMs: Date.now() + FILE_SETTLE_DELAY_MS
      });
      if (resolution.outcome === "reconcile_takeover") {
        this.#failureReporter?.reportJournalFailure(resolution.diagnosticReason);
      }
    }
  }
  /**
   * Whether a path is lifecycle-deferred: guarded in this session, durably
   * carrying a `deferred_lifecycle` event in the journal, or durably
   * reserved as an explicit-restore target (`restore_pending` — the
   * reservation-first protocol: staged restore bytes at the target must
   * never converge as a fresh source, and the reserved row's open
   * tombstone must not trigger the automatic-restore detector). Such a
   * path is never re-captured and excluded from the snapshot scan until
   * the owning lifecycle outcome releases it — the rename/move commit
   * deletes the marker rows (fix round 2 D7), the restore commit / the
   * explicit cancel advances the row out of `restore_pending`. A
   * terminally-failed rename keeps the marker (fail-closed, child 6
   * owns repair).
   */
  #isLifecycleDeferredPath(normalizedPath) {
    if (this.#lifecycleGuardedPaths.has(normalizedPath)) {
      return true;
    }
    const trackedFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (trackedFile === null) {
      return false;
    }
    if (this.#readLifecycleStateOf(trackedFile.localFileId) === "restore_pending") {
      return true;
    }
    return this.#repository.readEventsByLocalFileId(trackedFile.localFileId).some((event) => event.state === "deferred_lifecycle");
  }
  /** Read the closed `lifecycle_state` of one tracked file (or null). */
  #readLifecycleStateOf(localFileId) {
    try {
      const row = this.#repository.lifecycle.database.readAll(
        `select lifecycle_state from local_files where local_file_id = '${localFileId}';`
      )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
  }
  /** Read the open tombstone id of one tracked file (or null). */
  #readOpenTombstoneId(localFileId) {
    try {
      const row = this.#repository.lifecycle.database.readAll(
        `select open_tombstone_id from local_files where local_file_id = '${localFileId}';`
      )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
  }
  /** Normalize one Vault path to the canonical locator, or drop it closed. */
  #normalizePathOrNull(path) {
    if (typeof path !== "string") {
      return null;
    }
    try {
      return normalizePolicyLocator(path);
    } catch {
      return null;
    }
  }
};

// src/journal/automatic-snapshot.ts
var CoalescingQueuePassDispatcher = class {
  #runner;
  #failureReporter;
  #hasFollowUpPass = false;
  #isStopped = false;
  #drainPromise = null;
  constructor(runner, failureReporter = null) {
    this.#runner = runner;
    this.#failureReporter = failureReporter;
  }
  request() {
    if (this.#isStopped) {
      return Promise.resolve();
    }
    this.#hasFollowUpPass = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => {
        this.#failureReporter?.reportJournalFailure("queue_drain_failed");
        return void 0;
      });
      this.#drainPromise = drainPromise;
      void drainPromise.finally(() => {
        if (this.#drainPromise === drainPromise) {
          this.#drainPromise = null;
        }
      });
    }
    return this.#drainPromise;
  }
  stop() {
    this.#isStopped = true;
    this.#hasFollowUpPass = false;
    return this.#drainPromise ?? Promise.resolve();
  }
  async #drain() {
    while (!this.#isStopped && this.#hasFollowUpPass) {
      this.#hasFollowUpPass = false;
      const summary = await this.#runner.runPass();
      if (!this.#isStopped && summary.outcome === "deadline_reached") {
        this.#hasFollowUpPass = true;
      }
    }
  }
};
async function refreshVerifiedPolicyAndRequestSnapshot(options) {
  const previousRevisionNumber = options.readAcceptedRevisionNumber();
  await options.refresh();
  const acceptedRevisionNumber = options.readAcceptedRevisionNumber();
  if (previousRevisionNumber !== null && acceptedRevisionNumber !== null && acceptedRevisionNumber > previousRevisionNumber) {
    options.requestSnapshot("policy_revision_advanced");
  }
}
var AutomaticSnapshotCoordinator = class {
  #runner;
  #failureReporter;
  #hasFollowUpSnapshot = false;
  #isStopped = false;
  #abortController = new AbortController();
  #drainPromise = null;
  constructor(runner, failureReporter = null) {
    this.#runner = runner;
    this.#failureReporter = failureReporter;
  }
  request(reason) {
    void reason;
    if (this.#isStopped) return;
    this.#hasFollowUpSnapshot = true;
    if (this.#drainPromise === null) {
      const drainPromise = this.#drain().catch(() => {
        this.#failureReporter?.reportJournalFailure("snapshot_drain_failed");
        return void 0;
      });
      this.#drainPromise = drainPromise;
      void drainPromise.finally(() => {
        if (this.#drainPromise === drainPromise) {
          this.#drainPromise = null;
        }
      });
    }
  }
  stop() {
    this.#isStopped = true;
    this.#hasFollowUpSnapshot = false;
    this.#abortController.abort();
    return this.#drainPromise ?? Promise.resolve();
  }
  async #drain() {
    while (!this.#isStopped && this.#hasFollowUpSnapshot) {
      this.#hasFollowUpSnapshot = false;
      const result = await this.#runner.runSnapshot(this.#abortController.signal);
      if (!this.#isStopped && result.outcome === "completed" && result.queuedEventCount > 0) {
        await this.#runner.requestQueuePass();
      }
    }
  }
};
var PERIODIC_RECONCILE_INTERVAL_MS = 6 * 60 * 60 * 1e3;

// src/journal/lifecycle-repository.ts
var UUID_PATTERN3 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var PendingRenameIntentConflictError = class extends Error {
  reason = "pending_rename_intent_conflict";
  constructor() {
    super("pending rename intent conflict");
    this.name = "PendingRenameIntentConflictError";
  }
};
function sqlText(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
function isUuid(value) {
  return typeof value === "string" && UUID_PATTERN3.test(value);
}
function firstRow(result) {
  return result[0]?.values[0] ?? null;
}
function isNullableUuid(value) {
  return value === null || typeof value === "string" && UUID_PATTERN3.test(value);
}
function isNullableText(value) {
  return value === null || typeof value === "string";
}
function isNullableNonNegativeInteger(value) {
  return value === null || typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function isPositiveInteger2(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}
function validateNormalizedPath(normalizedPath) {
  if (typeof normalizedPath !== "string" || normalizedPath.length === 0 || normalizedPath.normalize("NFC") !== normalizedPath || normalizedPath.includes("\\") || normalizedPath.startsWith("/") || normalizedPath.endsWith("/")) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const character of normalizedPath) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 32 || codeUnit === 127) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  const segments = normalizedPath.split("/");
  if (segments[0]?.includes(":")) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function parsePendingRenameIntentRow(row) {
  const [localFileId, priorPath, currentPath] = row;
  if (typeof localFileId !== "string" || !isUuid(localFileId) || typeof priorPath !== "string" || priorPath.length === 0 || typeof currentPath !== "string" || currentPath.length === 0) {
    throw journalStoreError("journal_image_invalid");
  }
  return { localFileId, priorPath, currentPath };
}
function parentPath(normalizedPath) {
  const separatorIndex = normalizedPath.lastIndexOf("/");
  return separatorIndex < 0 ? "" : normalizedPath.slice(0, separatorIndex);
}
function isClosedStateToken(value) {
  return typeof value === "string" && JOURNAL_EVENT_STATES.includes(value);
}
function isClosedSafeErrorLabel(value) {
  return typeof value === "string" && JOURNAL_SAFE_ERROR_LABELS.includes(value);
}
function parseStoredEventRow(row) {
  const [
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    sha256,
    sizeBytes,
    mediaType,
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId
  ] = row;
  if (typeof eventId !== "string" || !isUuid(eventId) || typeof localFileId !== "string" || typeof idempotencyKey !== "string" || typeof operation !== "string" || !JOURNAL_OPERATIONS.includes(operation) || typeof sha256 !== "string" || typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 0 || typeof mediaType !== "string" || typeof state !== "string" || !isClosedStateToken(state) || typeof attemptCount !== "number" || !Number.isInteger(attemptCount) || attemptCount < 0 || !isNullableNonNegativeInteger(nextEligibleRetryEpochMs) || safeError !== null && !isClosedSafeErrorLabel(safeError) || operationId !== null && typeof operationId !== "string") {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    fingerprint: { sha256, sizeBytes, mediaType },
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId
  };
}
function parseLifecycleOperandRow(row) {
  const [
    operation,
    sourceId,
    expectedVersionId,
    expectedLocator,
    targetLocator,
    tombstoneId,
    policyRevision,
    predecessorEventId
  ] = row;
  if (typeof operation !== "string" || !isLifecycleJournalOperation(operation) || typeof sourceId !== "string" || !isUuid(sourceId) || typeof expectedVersionId !== "string" || !isUuid(expectedVersionId) || !isNullableText(expectedLocator) || !isNullableText(targetLocator) || !isNullableUuid(tombstoneId) || !isPositiveInteger2(policyRevision) || !isNullableUuid(predecessorEventId)) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    operation,
    sourceId,
    expectedVersionId,
    expectedLocator,
    targetLocator,
    tombstoneId,
    policyRevision,
    predecessorEventId,
    // The lifecycle_event_operands table does not store the captured
    // fingerprint; it lives on local_files as observed_* and is only
    // relevant at capture time (rename/move rebind). The driver reads
    // the operands row alone, so the captured triple is always null
    // here.
    capturedFingerprintSha256: null,
    capturedFingerprintSizeBytes: null,
    capturedFingerprintMediaType: null
  };
}
function initialStateFor(operation, override) {
  if (override !== void 0) {
    return override;
  }
  switch (operation) {
    case "rename":
      return "rename_pending";
    case "move":
      return "move_pending";
    case "delete":
      return "tombstoned";
    case "restore":
      return "restore_pending";
  }
}
var LIFECYCLE_FINGERPRINT = {
  sha256: "0".repeat(64),
  sizeBytes: 0,
  mediaType: "application/octet-stream"
};
function validateOptions(operands, options) {
  if (!isLifecycleJournalOperation(operands.operation)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid(operands.sourceId) || !isUuid(operands.expectedVersionId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (operands.predecessorEventId !== null && !isUuid(operands.predecessorEventId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isLifecycleLocalFileState(options.initialLifecycleState ?? initialStateFor(operands.operation, void 0))) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid(options.localFile.localFileId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (options.tombstoneId !== void 0 && options.tombstoneId !== null && !isUuid(options.tombstoneId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (operands.operation === "rename" || operands.operation === "move") {
    if (operands.expectedLocator === null || operands.targetLocator === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.expectedLocator === operands.targetLocator) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.tombstoneId !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  if (operands.operation === "delete") {
    if (operands.expectedLocator === null || operands.targetLocator !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (options.tombstoneId === void 0 || options.tombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  if (operands.operation === "restore") {
    if (operands.expectedLocator !== null || operands.targetLocator === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.tombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (operands.predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
}
var LifecycleRepository = class {
  #database;
  #createId;
  #nowEpochMs;
  constructor(options) {
    this.#database = options.database;
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  }
  /** The serialized database slice the lifecycle repository writes against. */
  get database() {
    return this.#database;
  }
  /** Read the durable rename chain owned by one stable local-file identity. */
  readPendingRenameIntentForLocalFile(localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select local_file_id, prior_path, current_path",
          "from pending_rename_intents",
          `where local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      )
    );
    return row === null ? null : parsePendingRenameIntentRow(row);
  }
  /** Read the unique durable owner of one latest observed Vault path. */
  readPendingRenameIntentByCurrentPath(currentPath) {
    validateNormalizedPath(currentPath);
    const row = firstRow(
      this.#database.readAll(
        [
          "select local_file_id, prior_path, current_path",
          "from pending_rename_intents",
          `where current_path = ${sqlText(currentPath)};`
        ].join(" ")
      )
    );
    return row === null ? null : parsePendingRenameIntentRow(row);
  }
  /**
   * Read the intent reserving either endpoint. Current-path ownership is
   * checked first because it is the authoritative watcher-ingress edge.
   */
  readPendingRenameIntentOwningEndpoint(normalizedPath) {
    validateNormalizedPath(normalizedPath);
    const rows = this.#database.readAll(
      [
        "select local_file_id, prior_path, current_path",
        "from pending_rename_intents",
        `where current_path = ${sqlText(normalizedPath)}`,
        `or prior_path = ${sqlText(normalizedPath)}`,
        "order by case when current_path =",
        `${sqlText(normalizedPath)} then 0 else 1 end, rowid asc limit 1;`
      ].join(" ")
    );
    const row = firstRow(rows);
    return row === null ? null : parsePendingRenameIntentRow(row);
  }
  /** Enumerate the bounded durable intent set for restart re-arming. */
  readPendingRenameIntents() {
    const result = this.#database.readAll(
      [
        "select local_file_id, prior_path, current_path",
        "from pending_rename_intents order by rowid asc;"
      ].join(" ")
    );
    return (result[0]?.values ?? []).map(parsePendingRenameIntentRow);
  }
  /**
   * Create or compose one observed rename edge under the stable local row.
   * The row path remains the canonical prior until an immutable lifecycle
   * prefix is materialized. An incompatible edge or reserved target commits
   * row-level reconciliation and only then raises the closed conflict token.
   */
  async recordOrComposePendingRenameIntent(input) {
    if (!isUuid(input.localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    validateNormalizedPath(input.observedPriorPath);
    validateNormalizedPath(input.observedCurrentPath);
    if (input.observedPriorPath === input.observedCurrentPath) {
      throw journalStoreError("journal_mutation_failed");
    }
    const outcome = await this.#database.runSerializedMutation((session) => {
      const localRow = firstRow(
        session.readRows(
          [
            "select normalized_path, lifecycle_state from local_files",
            `where local_file_id = ${sqlText(input.localFileId)};`
          ].join(" ")
        )
      );
      if (localRow === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [rowPath, lifecycleState] = localRow;
      if (typeof rowPath !== "string" || typeof lifecycleState !== "string") {
        throw journalStoreError("journal_image_invalid");
      }
      if (lifecycleState === "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }
      const storedRow = firstRow(
        session.readRows(
          [
            "select local_file_id, prior_path, current_path",
            "from pending_rename_intents",
            `where local_file_id = ${sqlText(input.localFileId)};`
          ].join(" ")
        )
      );
      const stored = storedRow === null ? null : parsePendingRenameIntentRow(storedRow);
      const targetOwner = firstRow(
        session.readRows(
          [
            "select local_file_id from local_files",
            `where normalized_path = ${sqlText(input.observedCurrentPath)}`,
            `and local_file_id <> ${sqlText(input.localFileId)} limit 1;`
          ].join(" ")
        )
      );
      const targetIntentOwner = firstRow(
        session.readRows(
          [
            "select local_file_id from pending_rename_intents",
            `where current_path = ${sqlText(input.observedCurrentPath)}`,
            `and local_file_id <> ${sqlText(input.localFileId)} limit 1;`
          ].join(" ")
        )
      );
      const isTargetReserved = targetOwner !== null || targetIntentOwner !== null;
      if (stored === null) {
        if (rowPath !== input.observedPriorPath || isTargetReserved) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            input.localFileId,
            rowPath
          );
          return "conflict";
        }
        session.exec(
          [
            "insert into pending_rename_intents",
            "(local_file_id, prior_path, current_path) values (",
            `${sqlText(input.localFileId)},`,
            `${sqlText(input.observedPriorPath)},`,
            `${sqlText(input.observedCurrentPath)});`
          ].join(" ")
        );
        return "created";
      }
      if (stored.priorPath === input.observedPriorPath && stored.currentPath === input.observedCurrentPath) {
        return "unchanged";
      }
      if (stored.currentPath !== input.observedPriorPath || isTargetReserved) {
        this.#reconcilePendingRenameIntentInSession(
          session,
          input.localFileId,
          stored.currentPath
        );
        return "conflict";
      }
      if (input.observedCurrentPath === stored.priorPath) {
        const openPrefixCount = this.#readOpenRenamePrefixCountInSession(
          session,
          input.localFileId
        );
        if (openPrefixCount === 0) {
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(input.localFileId)};`
          );
          session.exec(
            `delete from pending_rename_intents where local_file_id = ${sqlText(input.localFileId)};`
          );
          return "cancelled";
        }
        if (openPrefixCount !== 1) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            input.localFileId,
            stored.currentPath
          );
          return "conflict";
        }
        session.exec(
          [
            "update pending_rename_intents set",
            `current_path = ${sqlText(stored.priorPath)}`,
            `where local_file_id = ${sqlText(input.localFileId)};`
          ].join(" ")
        );
        return "compensation_pending";
      }
      session.exec(
        [
          "update pending_rename_intents set",
          `current_path = ${sqlText(input.observedCurrentPath)}`,
          `where local_file_id = ${sqlText(input.localFileId)};`
        ].join(" ")
      );
      return "composed";
    });
    if (outcome === "conflict") {
      throw new PendingRenameIntentConflictError();
    }
    return outcome;
  }
  /**
   * Materialize the latest durable endpoints as one immutable lifecycle
   * prefix, freezing content and rebinding the local row in the same writer.
   * A row without committed source/base identity remains parked as an intent.
   */
  async recordPendingRenameLifecycleEvent(localFileId, capturedFingerprint) {
    if (!isUuid(localFileId) || !isFrozenFingerprintShape(capturedFingerprint)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#database.runSerializedMutation((session) => {
      const intentRow = firstRow(
        session.readRows(
          [
            "select local_file_id, prior_path, current_path",
            "from pending_rename_intents",
            `where local_file_id = ${sqlText(localFileId)};`
          ].join(" ")
        )
      );
      if (intentRow === null) {
        return { kind: "result", value: null };
      }
      const intent = parsePendingRenameIntentRow(intentRow);
      const openPrefix = this.#readOpenRenamePrefixInSession(session, localFileId);
      if (openPrefix !== null) {
        if (openPrefix.operands.expectedLocator !== intent.priorPath) {
          this.#reconcilePendingRenameIntentInSession(
            session,
            localFileId,
            intent.currentPath
          );
          return { kind: "conflict" };
        }
        return { kind: "result", value: openPrefix.result };
      }
      if (intent.priorPath === intent.currentPath) {
        this.#reconcilePendingRenameIntentInSession(
          session,
          localFileId,
          intent.currentPath
        );
        return { kind: "conflict" };
      }
      const localRow = firstRow(
        session.readRows(
          [
            "select normalized_path, source_id, observed_sha256, observed_size_bytes,",
            "observed_media_type, base_version_id, policy_revision, lifecycle_state,",
            "last_committed_sha256, last_committed_size_bytes, last_committed_media_type",
            "from local_files",
            `where local_file_id = ${sqlText(localFileId)};`
          ].join(" ")
        )
      );
      if (localRow === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [
        normalizedPath,
        sourceId,
        observedSha256,
        observedSizeBytes,
        observedMediaType,
        baseVersionId,
        policyRevision,
        lifecycleState,
        committedSha256,
        committedSizeBytes,
        committedMediaType
      ] = localRow;
      if (lifecycleState === "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }
      if (sourceId === null || baseVersionId === null) {
        return { kind: "result", value: null };
      }
      if (typeof normalizedPath !== "string" || typeof sourceId !== "string" || typeof observedSha256 !== "string" || typeof observedSizeBytes !== "number" || typeof observedMediaType !== "string" || typeof baseVersionId !== "string" || typeof policyRevision !== "number" || !Number.isInteger(policyRevision) || policyRevision < 1) {
        throw journalStoreError("journal_image_invalid");
      }
      const lastCommittedFingerprint = committedSha256 === null && committedSizeBytes === null && committedMediaType === null ? null : typeof committedSha256 === "string" && typeof committedSizeBytes === "number" && typeof committedMediaType === "string" ? {
        sha256: committedSha256,
        sizeBytes: committedSizeBytes,
        mediaType: committedMediaType
      } : null;
      if (lastCommittedFingerprint === null && (committedSha256 !== null || committedSizeBytes !== null || committedMediaType !== null)) {
        throw journalStoreError("journal_image_invalid");
      }
      const localFile = {
        localFileId,
        normalizedPath,
        sourceId,
        observedFingerprint: {
          sha256: observedSha256,
          sizeBytes: observedSizeBytes,
          mediaType: observedMediaType
        },
        baseVersionId,
        policyRevisionNumber: policyRevision,
        lastCommittedFingerprint
      };
      const operation = parentPath(intent.priorPath) === parentPath(intent.currentPath) ? "rename" : "move";
      const operands = {
        operation,
        sourceId,
        expectedVersionId: baseVersionId,
        expectedLocator: intent.priorPath,
        targetLocator: intent.currentPath,
        tombstoneId: null,
        policyRevision,
        predecessorEventId: null,
        capturedFingerprintSha256: capturedFingerprint.sha256,
        capturedFingerprintSizeBytes: capturedFingerprint.sizeBytes,
        capturedFingerprintMediaType: capturedFingerprint.mediaType
      };
      this.#freezePendingForLocalFileInSession(session, localFileId);
      session.exec(
        [
          "delete from multipart_upload_progress where event_id in (",
          "select event_id from journal_events",
          `where local_file_id = ${sqlText(localFileId)}`,
          "and state = 'deferred_lifecycle');"
        ].join(" ")
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`
      );
      const value = this.#recordLifecycleEventInSession(session, {
        operands,
        localFile,
        tombstoneId: null,
        newPath: intent.currentPath,
        forceFailureAfterExec: false
      });
      return { kind: "result", value };
    });
    if (result.kind === "conflict") {
      throw new PendingRenameIntentConflictError();
    }
    return result.value;
  }
  /**
   * Atomically reparent the owner to its latest durable intent endpoint and
   * release the intent/counter reservation. Row-specific reconciliation owns
   * locator truth after this exit, so no stale endpoint may block admission.
   */
  async reparentAndClearPendingRenameIntent(localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      this.#reparentAndClearPendingRenameIntentInSession(session, localFileId);
    });
  }
  /**
   * Consume an exact echo marker and release its pending rename reservation
   * in the same writer. If the owner reparent cannot commit, the marker
   * deletion rolls back with it.
   */
  async consumePendingRenameEchoAndReparent(localFileId, consumeEchoInSession) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      if (!consumeEchoInSession(session)) {
        return false;
      }
      this.#reparentAndClearPendingRenameIntentInSession(session, localFileId);
      return true;
    });
  }
  /**
   * The only direct terminal exit for a pending rename/move prefix. It keeps
   * attempt audit, event close, missing-file counter cleanup, owner reparent
   * and reconciliation transfer inside the same SQLite mutation.
   */
  async resolveIntentAwareLifecycleTerminal(input) {
    if (!isUuid(input.eventId) || !isPositiveInteger2(input.attemptedAtEpochMs) || typeof input.requestCorrelationId !== "string" || input.requestCorrelationId.length === 0 || input.requestCorrelationId.length > 128) {
      throw journalStoreError("journal_mutation_failed");
    }
    for (const character of input.requestCorrelationId) {
      const codeUnit = character.charCodeAt(0);
      if (codeUnit < 32 || codeUnit > 126) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    return this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select je.event_id, je.local_file_id, je.idempotency_key, je.operation,",
            "je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,",
            "je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id",
            "from journal_events je join lifecycle_event_operands leo",
            "on leo.event_id = je.event_id",
            `where je.event_id = ${sqlText(input.eventId)};`
          ].join(" ")
        )
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const event = parseStoredEventRow(row);
      if (event.operation !== "rename" && event.operation !== "move" || !JOURNAL_PENDING_EVENT_STATES.includes(event.state)) {
        throw journalStoreError("journal_mutation_failed");
      }
      this.#recordLifecycleAttemptInSession(session, {
        eventId: event.eventId,
        attemptedAtEpochMs: input.attemptedAtEpochMs,
        outcomeLabel: input.terminalState,
        requestCorrelationId: input.requestCorrelationId
      });
      session.exec(
        [
          "update journal_events set",
          `state = ${sqlText(input.terminalState)},`,
          "next_eligible_retry_epoch_ms = null,",
          `safe_error = ${sqlText(input.terminalState)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText(event.eventId)};`
        ].join(" ")
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText(event.eventId)};`
      );
      const intent = firstRow(
        session.readRows(
          [
            "select current_path from pending_rename_intents",
            `where local_file_id = ${sqlText(event.localFileId)};`
          ].join(" ")
        )
      );
      if (intent === null) {
        return "no_intent";
      }
      const currentPath = intent[0];
      if (typeof currentPath !== "string" || currentPath.length === 0) {
        throw journalStoreError("journal_image_invalid");
      }
      this.#reconcilePendingRenameIntentInSession(session, event.localFileId, currentPath);
      return "intent_reconciled";
    });
  }
  /**
   * Record one lifecycle event in one transaction: a `journal_events`
   * row, a `lifecycle_event_operands` row keyed by `event_id` and the
   * `local_files` row update (`last_locator`, `open_tombstone_id`,
   * `lifecycle_state`, and for `rename`/`move` the captured-fingerprint
   * columns + the `normalized_path` rebind). On any failure the entire
   * transaction rolls back, so partial writes never reach a verified
   * generation.
   *
   * Idempotency: replaying an existing event with the same
   * `(localFileId, operation, expectedVersionId, expectedLocator,
   * targetLocator, tombstoneId, predecessorEventId, policyRevision)`
   * tuple returns the original event without inserting a duplicate row.
   */
  async recordLifecycleEvent(operands, options = { localFile: { localFileId: "" } }) {
    validateOptions(operands, options);
    return this.#database.runSerializedMutation(
      (session) => this.#recordLifecycleEventInSession(session, {
        operands,
        localFile: options.localFile,
        tombstoneId: options.tombstoneId ?? null,
        newPath: options.newPath ?? null,
        forceFailureAfterExec: options.forceFailureAfterExec === true
      })
    );
  }
  /**
   * The atomic lifecycle event writer used by the rename / move / delete /
   * restore capture path (spec 7.1 fix round 1 I1). It runs three
   * mutations in one transaction:
   *
   *   1. freeze every still-pending content event of the tracked file
   *      as `deferred_lifecycle` so no later queue pass selects it;
   *   2. insert the `journal_events` row, the `lifecycle_event_operands`
   *      row and the matching `local_files` update via
   *      {@link recordLifecycleEventInSession};
   *   3. write the `local_files.normalized_path` rebind (for
   *      `rename` / `move`) atomically inside the same writer call.
   *
   * A throwing operation rolls back the whole transaction so a torn
   * rename never escapes a verified generation.
   */
  async recordLifecycleEventWithFreeze(input) {
    return this.#database.runSerializedMutation((session) => {
      this.#freezePendingForLocalFileInSession(
        session,
        input.localFile.localFileId
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(input.localFile.localFileId)};`
      );
      return this.#recordLifecycleEventInSession(session, {
        operands: input.operands,
        localFile: input.localFile,
        tombstoneId: input.tombstoneId ?? null,
        newPath: input.newPath ?? null,
        forceFailureAfterExec: input.forceFailureAfterExec === true
      });
    });
  }
  /**
   * Reserve one explicit-restore target locator (the reservation-first
   * protocol of the explicit-restore target reservation spec): in ONE
   * transaction the tombstoned row is rebound to the target path and
   * enters `restore_pending`, with the pre-reservation path retained in
   * `restore_prior_path` for an explicit cancel and for the committed
   * receipt's cleanup.
   *
   * Target availability is the precondition the upstream server contract
   * demands ("an explicitly requested, available target locator"):
   *   - another row WITH a source identity at the target → refused
   *     `restore_target_occupied` (a converged fresh source or a genuine
   *     other note; both rows stay untouched);
   *   - a phantom row (no source identity) whose content events are all
   *     unsent (`queued` / `waiting_retry`) → the phantom mapping and its
   *     never-shipped events are released inside this same transaction
   *     (the `removeLocalMapping` cleanup shape) and the reservation
   *     proceeds — staged restore bytes must never converge as a fresh
   *     source;
   *   - a phantom row with any event in `preflight` / `uploading` →
   *     refused `restore_target_busy` (retry after the pass settles).
   *
   * A non-terminal `restore` event of the row refuses the reservation as
   * `restore_already_pending`. Re-reservation from `restore_pending`
   * preserves the original `restore_prior_path` (never chained). A row
   * without an open tombstone, source identity or stored predecessor
   * delete event fails closed as `journal_mutation_failed`.
   */
  async reserveRestoreTarget(localFileId, targetPath) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (typeof targetPath !== "string" || targetPath.length === 0) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select lifecycle_state, open_tombstone_id, source_id, base_version_id,",
            "normalized_path, restore_prior_path from local_files",
            `where local_file_id = ${sqlText(localFileId)};`
          ].join(" ")
        )
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [lifecycleState, openTombstoneId, sourceId, baseVersionId, currentPath, existingPriorPath] = row;
      if (typeof lifecycleState !== "string" || typeof currentPath !== "string" || sourceId === null || baseVersionId === null || typeof sourceId !== "string" || typeof baseVersionId !== "string" || typeof openTombstoneId !== "string" || openTombstoneId.length === 0) {
        throw journalStoreError("journal_mutation_failed");
      }
      if (lifecycleState !== "tombstoned" && lifecycleState !== "restore_pending") {
        throw journalStoreError("journal_mutation_failed");
      }
      const predecessor = firstRow(
        session.readRows(
          [
            "select event_id from journal_events",
            `where local_file_id = ${sqlText(localFileId)}`,
            "and operation = 'delete' limit 1;"
          ].join(" ")
        )
      );
      if (predecessor === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const inFlightRestore = firstRow(
        session.readRows(
          [
            "select journal_events.event_id from journal_events",
            "join lifecycle_event_operands",
            "on lifecycle_event_operands.event_id = journal_events.event_id",
            `where journal_events.local_file_id = ${sqlText(localFileId)}`,
            "and journal_events.operation = 'restore'",
            "and journal_events.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
            "limit 1;"
          ].join(" ")
        )
      );
      if (inFlightRestore !== null) {
        return { outcome: "refused", reason: "restore_already_pending" };
      }
      const reservedIntentEndpoint = firstRow(
        session.readRows(
          [
            "select local_file_id from pending_rename_intents",
            `where prior_path = ${sqlText(targetPath)}`,
            `or current_path = ${sqlText(targetPath)}`,
            "limit 1;"
          ].join(" ")
        )
      );
      if (reservedIntentEndpoint !== null) {
        return { outcome: "refused", reason: "restore_target_busy" };
      }
      const occupant = firstRow(
        session.readRows(
          [
            "select local_file_id, source_id from local_files",
            `where normalized_path = ${sqlText(targetPath)};`
          ].join(" ")
        )
      );
      if (occupant !== null && occupant[0] !== localFileId) {
        if (typeof occupant[1] === "string" && occupant[1].length > 0) {
          return { outcome: "refused", reason: "restore_target_occupied" };
        }
        const occupantId = String(occupant[0]);
        const inFlightUpload = firstRow(
          session.readRows(
            [
              "select event_id from journal_events",
              `where local_file_id = ${sqlText(occupantId)}`,
              "and state in ('preflight', 'uploading') limit 1;"
            ].join(" ")
          )
        );
        if (inFlightUpload !== null) {
          return { outcome: "refused", reason: "restore_target_busy" };
        }
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(occupantId)};`
        );
        session.exec(
          `delete from pending_rename_intents where local_file_id = ${sqlText(occupantId)};`
        );
        session.exec(
          `delete from journal_attempts where event_id in (select event_id from journal_events where local_file_id = ${sqlText(occupantId)});`
        );
        session.exec(
          `delete from lifecycle_event_operands where event_id in (select event_id from journal_events where local_file_id = ${sqlText(occupantId)});`
        );
        session.exec(
          `delete from journal_events where local_file_id = ${sqlText(occupantId)};`
        );
        session.exec(
          `delete from local_files where local_file_id = ${sqlText(occupantId)};`
        );
      }
      const priorPath = lifecycleState === "tombstoned" || typeof existingPriorPath !== "string" || existingPriorPath.length === 0 ? currentPath : existingPriorPath;
      session.exec(
        [
          "update local_files set",
          `normalized_path = ${sqlText(targetPath)},`,
          "lifecycle_state = 'restore_pending',",
          `restore_prior_path = ${sqlText(priorPath)}`,
          `where local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      );
      return { outcome: "reserved", priorNormalizedPath: priorPath };
    });
  }
  /**
   * Release one explicit-restore reservation (the explicit Cancel path of
   * the restore command): in ONE transaction the row returns to its
   * pre-reservation path, the state returns to `tombstoned` (the open
   * tombstone was retained through the reservation) and
   * `restore_prior_path` clears. A row that is not `restore_pending`, or
   * whose prior path was not retained, fails closed as
   * `journal_mutation_failed`.
   */
  async releaseRestoreTarget(localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const row = firstRow(
        session.readRows(
          [
            "select lifecycle_state, restore_prior_path from local_files",
            `where local_file_id = ${sqlText(localFileId)};`
          ].join(" ")
        )
      );
      if (row === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [lifecycleState, priorPath] = row;
      if (lifecycleState !== "restore_pending" || typeof priorPath !== "string" || priorPath.length === 0) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `normalized_path = ${sqlText(priorPath)},`,
          "lifecycle_state = 'tombstoned',",
          "restore_prior_path = null",
          `where local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Fail-closed reconcile flagger used when a tombstoned path re-appears
   * with bytes that no longer match the last-committed fingerprint
   * (spec 7.1 fix round 1 C2). The row's lifecycle state flips to
   * `reconcile_required`, the open tombstone is cleared so the file is
   * no longer eligible for automatic restore, and the global
   * `journal_meta.is_reconcile_required` flag is set so a later pass
   * knows to recover the row.
   */
  async recordLifecycleReconcileForLocalFile(localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          [
            "select lf.normalized_path, pri.current_path from local_files lf",
            "left join pending_rename_intents pri",
            "on pri.local_file_id = lf.local_file_id",
            `where lf.local_file_id = ${sqlText(localFileId)};`
          ].join(" ")
        )
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [rowPath, intentCurrentPath] = existing;
      if (typeof rowPath !== "string" || intentCurrentPath !== null && typeof intentCurrentPath !== "string") {
        throw journalStoreError("journal_image_invalid");
      }
      this.#reconcilePendingRenameIntentInSession(
        session,
        localFileId,
        intentCurrentPath ?? rowPath
      );
    });
  }
  /**
   * Mark one `local_files` row as tombstoned. The focused helper is
   * used by the capture flow when the server has confirmed a delete
   * before the lifecycle event has reached the durable journal (or
   * when the durable event has been pruned but the local mapping must
   * stay). All mutations run inside one transaction.
   */
  async markTombstoneForLocalFile(localFileId, tombstoneId) {
    if (!isUuid(localFileId) || !isUuid(tombstoneId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`
        )
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `open_tombstone_id = ${sqlText(tombstoneId)},`,
          `lifecycle_state = 'tombstoned'`,
          `where local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Clear the retained tombstone on one `local_files` row after a
   * restore successor has been committed. The lifecycle state returns
   * to `restored`; the rest of the row stays intact so a future save
   * can resume the content surface.
   */
  async consumeRestoreSuccessor(localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`
        )
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update local_files set",
          `open_tombstone_id = null,`,
          `lifecycle_state = 'restored'`,
          `where local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Persist the safe receipt of one committed lifecycle event in
   * one transaction: the event flips to terminal `committed`, the
   * `local_files.lifecycle_state` advances past the pending state
   * for the closed operation, and the server-returned tombstone id
   * (when present) replaces any locally-staged value so the durable
   * record is exactly what the server acknowledged (spec 19.2
   * exact-replay rule; task 9 fix round 1 I1).
   *
   * The last_committed_* columns are intentionally left untouched:
   * a rename / move / delete / restore does not change file bytes,
   * so the prior `last_committed_*` triple stays provable for the
   * next restore-eligibility check.
   *
   * The nullable `serverReceipt` argument carries the server-
   * returned tombstone id when the operation is `delete` (or
   * `restore` reporting a server-derived tombstone identity). When
   * the receipt is omitted or its `tombstoneId` is null, the existing
   * `lifecycle_event_operands.tombstone_id` column is preserved (a
   * non-`delete` commit never overwrites the tombstone identity).
   */
  async recordLifecycleCommittedReceipt(eventId, serverReceipt = null) {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (serverReceipt !== null) {
      const tombstoneId = serverReceipt.tombstoneId;
      if (tombstoneId !== null && !isUuid(tombstoneId)) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    return this.#database.runSerializedMutation((session) => {
      const event = firstRow(
        session.readRows(
          [
            "select je.event_id, je.local_file_id, je.operation, leo.target_locator",
            "from journal_events je join lifecycle_event_operands leo",
            "on leo.event_id = je.event_id",
            `where je.event_id = ${sqlText(eventId)};`
          ].join(" ")
        )
      );
      if (event === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const [storedEventId, localFileId, operation, targetLocator] = event;
      if (typeof storedEventId !== "string" || typeof localFileId !== "string" || typeof operation !== "string") {
        throw journalStoreError("journal_query_failed");
      }
      if (operation !== "rename" && operation !== "move" && operation !== "delete" && operation !== "restore") {
        throw journalStoreError("journal_mutation_failed");
      }
      let pendingRenameIntentLocalFileId = null;
      session.exec(
        [
          "update journal_events set state = 'committed',",
          "next_eligible_retry_epoch_ms = null,",
          "safe_error = null",
          `where event_id = ${sqlText(eventId)};`
        ].join(" ")
      );
      if (serverReceipt !== null && serverReceipt.tombstoneId !== null) {
        session.exec(
          [
            "update lifecycle_event_operands set",
            `server_receipt_tombstone_id = ${sqlText(serverReceipt.tombstoneId)}`,
            `where event_id = ${sqlText(eventId)};`
          ].join(" ")
        );
      }
      switch (operation) {
        case "rename":
        case "move":
          if (typeof targetLocator !== "string") {
            throw journalStoreError("journal_image_invalid");
          }
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'active'",
              `where local_file_id = ${sqlText(localFileId)};`
            ].join(" ")
          );
          session.exec(
            [
              "delete from journal_attempts where event_id in (",
              "select event_id from journal_events",
              `where local_file_id = ${sqlText(localFileId)}`,
              "and state = 'deferred_lifecycle');"
            ].join(" ")
          );
          session.exec(
            [
              "delete from journal_events",
              `where local_file_id = ${sqlText(localFileId)}`,
              "and state = 'deferred_lifecycle';"
            ].join(" ")
          );
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`
          );
          const pendingIntentRow = firstRow(
            session.readRows(
              [
                "select local_file_id, prior_path, current_path",
                "from pending_rename_intents",
                `where local_file_id = ${sqlText(localFileId)};`
              ].join(" ")
            )
          );
          if (pendingIntentRow !== null) {
            const pendingIntent = parsePendingRenameIntentRow(pendingIntentRow);
            if (pendingIntent.currentPath === targetLocator) {
              session.exec(
                `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`
              );
            } else {
              session.exec(
                [
                  "update pending_rename_intents set",
                  `prior_path = ${sqlText(targetLocator)}`,
                  `where local_file_id = ${sqlText(localFileId)};`
                ].join(" ")
              );
              pendingRenameIntentLocalFileId = localFileId;
            }
          }
          break;
        case "delete":
          session.exec(
            `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`
          );
          session.exec(
            `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`
          );
          session.exec(
            [
              "update local_files set",
              "lifecycle_state = 'tombstoned'",
              `where local_file_id = ${sqlText(localFileId)};`
            ].join(" ")
          );
          break;
        case "restore":
          session.exec(
            [
              "update local_files set",
              "normalized_path = (select target_locator from lifecycle_event_operands",
              `where event_id = ${sqlText(eventId)}),`,
              "lifecycle_state = 'restored',",
              "restore_prior_path = null",
              `where local_file_id = ${sqlText(localFileId)};`
            ].join(" ")
          );
          break;
      }
      return { pendingRenameIntentLocalFileId };
    });
  }
  /**
   * Read the server-confirmed tombstone id of one committed lifecycle
   * event. The reader returns the `server_receipt_tombstone_id`
   * column the {@link recordLifecycleCommittedReceipt} mutator writes
   * on a successful `delete` commit. The restore driver uses this
   * read to override the operands-derived tombstone id on the wire
   * body so the server hears the same identity it returned on the
   * predecessor (task 9 fix round 1 I1).
   *
   * Returns `null` when the event has no persisted server receipt —
   * the predecessor's commit is still in flight, or the operation is
   * not `delete` / `restore`.
   */
  readServerReceiptTombstoneId(eventId) {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select server_receipt_tombstone_id",
          `from lifecycle_event_operands where event_id = ${sqlText(eventId)};`
        ].join(" ")
      )
    );
    if (row === null) {
      return null;
    }
    const [stored] = row;
    if (stored === null) {
      return null;
    }
    if (typeof stored !== "string" || !isUuid(stored)) {
      throw journalStoreError("journal_image_invalid");
    }
    return stored;
  }
  /**
   * The query the lifecycle driver uses to pick its next eligible
   * event: the oldest lifecycle row whose retry time has passed (or
   * with no retry scheduled) and whose predecessor, when one is
   * declared, is already terminal-success on the server. A
   * `predecessor_event_id` referencing an event that is missing or
   * still pending is deferred — the brief requires that the
   * successor MUST NOT dispatch until the predecessor is
   * terminal-success.
   *
   * Returns the closed `FrozenLifecycleEvent` (event + operands
   * pair) so the driver can ship both to the wire in one commit;
   * returns `null` when no eligible lifecycle event exists.
   */
  readOldestEligibleLifecycleEvent(nowEpochMs) {
    if (!isPositiveInteger2(nowEpochMs)) {
      throw journalStoreError("journal_query_failed");
    }
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map(
      (state2) => sqlText(state2)
    ).join(", ");
    const lifecycleOperations = [
      "rename",
      "move",
      "delete",
      "restore"
    ].map((value) => sqlText(value)).join(", ");
    const row = firstRow(
      this.#database.readAll(
        [
          `select je.event_id, je.local_file_id, je.idempotency_key, je.operation,`,
          `je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,`,
          `je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id,`,
          `leo.source_id, leo.expected_version_id,`,
          `leo.expected_locator, leo.target_locator, leo.tombstone_id,`,
          `leo.policy_revision, leo.predecessor_event_id`,
          `from journal_events je`,
          `join lifecycle_event_operands leo on leo.event_id = je.event_id`,
          `left join journal_events pe on pe.event_id = leo.predecessor_event_id`,
          `where je.operation in (${lifecycleOperations})`,
          `and ((je.state in (${coalescableStateList})`,
          `and (je.next_eligible_retry_epoch_ms is null`,
          `or je.next_eligible_retry_epoch_ms <= ${nowEpochMs}))`,
          `or je.state in ('preflight', 'uploading'))`,
          `and (leo.predecessor_event_id is null`,
          `or (pe.state = 'committed'))`,
          `order by je.created_at_epoch_ms asc, je.rowid asc limit 1;`
        ].join(" ")
      )
    );
    if (row === null) {
      return null;
    }
    const [
      eventId,
      localFileId,
      idempotencyKey,
      operation,
      sha256,
      sizeBytes,
      mediaType,
      state,
      attemptCount,
      nextEligibleRetryEpochMs,
      safeError,
      operationId,
      sourceId,
      expectedVersionId,
      expectedLocator,
      targetLocator,
      tombstoneId,
      policyRevision,
      predecessorEventId
    ] = row;
    const event = parseStoredEventRow([
      eventId,
      localFileId,
      idempotencyKey,
      operation,
      sha256,
      sizeBytes,
      mediaType,
      state,
      attemptCount,
      nextEligibleRetryEpochMs,
      safeError,
      operationId
    ]);
    const operands = parseLifecycleOperandRow([
      operation,
      sourceId,
      expectedVersionId,
      expectedLocator,
      targetLocator,
      tombstoneId,
      policyRevision,
      predecessorEventId
    ]);
    if (operands.operation !== event.operation) {
      throw journalStoreError("journal_image_invalid");
    }
    return { event, operands };
  }
  /**
   * Read the keyed operand row of one stored lifecycle event. The
   * driver uses this to look up the operands after a replay: when the
   * same `event_id` is selected again, the wire body must carry the
   * ORIGINAL operands (never a re-derived shape) so the server's
   * exact-replay contract holds. Returns `null` when no operand row
   * exists (an event without an operands row is the very condition
   * the reconcile-required flagger closes).
   */
  readLifecycleOperands(eventId) {
    if (!isUuid(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow(
      this.#database.readAll(
        [
          "select je.operation, leo.source_id, leo.expected_version_id, leo.expected_locator,",
          "leo.target_locator, leo.tombstone_id, leo.policy_revision, leo.predecessor_event_id",
          "from lifecycle_event_operands leo join journal_events je",
          "on je.event_id = leo.event_id",
          `where leo.event_id = ${sqlText(eventId)};`
        ].join(" ")
      )
    );
    if (row === null) {
      return null;
    }
    return parseLifecycleOperandRow(row);
  }
  /**
   * Find every local file whose dependency evidence is corrupt or
   * missing: a `predecessor_event_id` referencing a row no longer in
   * `journal_events`, a missing `lifecycle_event_operands` row for a
   * lifecycle event, or an `expected_version_id` that no longer matches
   * a stored source. The probe also durably sets
   * `journal_meta.is_reconcile_required = 1` inside the same
   * transaction so a stale dependency never goes unflagged.
   */
  async findReconcileRequired() {
    return this.#database.runSerializedMutation((session) => {
      const read = (sql) => session.readRows(sql);
      const flagged = [];
      const predecessorProbe = read(
        [
          "select lf.local_file_id, lf.normalized_path",
          "from local_files lf",
          "join journal_events je on je.local_file_id = lf.local_file_id",
          "left join lifecycle_event_operands leo",
          "  on leo.event_id = je.event_id",
          "left join journal_events pe",
          "  on pe.event_id = leo.predecessor_event_id",
          "where je.operation in ('rename', 'move', 'delete', 'restore')",
          "and leo.predecessor_event_id is not null",
          "and pe.event_id is null;"
        ].join(" ")
      );
      for (const row of predecessorProbe[0]?.values ?? []) {
        const [localFileId, normalizedPath] = row;
        if (typeof localFileId !== "string" || typeof normalizedPath !== "string") {
          throw journalStoreError("journal_query_failed");
        }
        flagged.push({
          localFileId,
          normalizedPath,
          reason: "predecessor_missing"
        });
      }
      const operandsProbe = read(
        [
          "select je.event_id, lf.local_file_id, lf.normalized_path",
          "from journal_events je",
          "join local_files lf on lf.local_file_id = je.local_file_id",
          "left join lifecycle_event_operands leo",
          "  on leo.event_id = je.event_id",
          "where je.operation in ('rename', 'move', 'delete', 'restore')",
          "and leo.event_id is null;"
        ].join(" ")
      );
      for (const row of operandsProbe[0]?.values ?? []) {
        const [, localFileId, normalizedPath] = row;
        if (typeof localFileId !== "string" || typeof normalizedPath !== "string") {
          throw journalStoreError("journal_query_failed");
        }
        if (!flagged.some((entry) => entry.localFileId === localFileId)) {
          flagged.push({
            localFileId,
            normalizedPath,
            reason: "operands_missing"
          });
        }
      }
      if (flagged.length > 0) {
        const meta = session.readJournalMeta();
        if (!meta.isReconcileRequired) {
          session.writeJournalMeta({ ...meta, isReconcileRequired: true });
        }
        const localFileIds = flagged.map((entry) => sqlText(entry.localFileId));
        session.exec(
          `update local_files set lifecycle_state = 'reconcile_required' where local_file_id in (${localFileIds.join(", ")});`
        );
      }
      return flagged;
    });
  }
  // --- internals ---------------------------------------------------------------------------
  #readOpenRenamePrefixCountInSession(session, localFileId) {
    const row = firstRow(
      session.readRows(
        [
          "select count(*) from journal_events",
          `where local_file_id = ${sqlText(localFileId)}`,
          "and operation in ('rename', 'move')",
          "and state in ('queued', 'preflight', 'uploading', 'waiting_retry');"
        ].join(" ")
      )
    );
    const count = row?.[0];
    if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
      throw journalStoreError("journal_image_invalid");
    }
    return count;
  }
  #readOpenRenamePrefixInSession(session, localFileId) {
    const count = this.#readOpenRenamePrefixCountInSession(session, localFileId);
    if (count === 0) {
      return null;
    }
    if (count !== 1) {
      throw journalStoreError("journal_image_invalid");
    }
    const row = firstRow(
      session.readRows(
        [
          "select je.event_id, je.local_file_id, je.idempotency_key, je.operation,",
          "je.sha256, je.size_bytes, je.media_type, je.state, je.attempt_count,",
          "je.next_eligible_retry_epoch_ms, je.safe_error, je.operation_id,",
          "lf.lifecycle_state, leo.source_id, leo.expected_version_id,",
          "leo.expected_locator, leo.target_locator, leo.tombstone_id,",
          "leo.policy_revision, leo.predecessor_event_id",
          "from journal_events je",
          "join lifecycle_event_operands leo on leo.event_id = je.event_id",
          "join local_files lf on lf.local_file_id = je.local_file_id",
          `where je.local_file_id = ${sqlText(localFileId)}`,
          "and je.operation in ('rename', 'move')",
          "and je.state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
          "order by je.created_at_epoch_ms asc, je.rowid asc limit 1;"
        ].join(" ")
      )
    );
    if (row === null) {
      throw journalStoreError("journal_image_invalid");
    }
    const event = parseStoredEventRow(row.slice(0, 12));
    const lifecycleState = row[12];
    const operands = parseLifecycleOperandRow([
      row[3],
      row[13],
      row[14],
      row[15],
      row[16],
      row[17],
      row[18],
      row[19]
    ]);
    if (typeof lifecycleState !== "string" || !isLifecycleLocalFileState(lifecycleState) || event.operation !== operands.operation) {
      throw journalStoreError("journal_image_invalid");
    }
    return {
      operands,
      result: {
        event,
        eventId: event.eventId,
        eventIdempotencyKey: event.idempotencyKey,
        lifecycleState
      }
    };
  }
  #reconcilePendingRenameIntentInSession(session, localFileId, currentPath) {
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`
    );
    session.exec(
      `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`
    );
    session.exec(
      [
        "update local_files set",
        `normalized_path = ${sqlText(currentPath)},`,
        "lifecycle_state = 'reconcile_required',",
        "open_tombstone_id = null",
        `where local_file_id = ${sqlText(localFileId)};`
      ].join(" ")
    );
    const meta = session.readJournalMeta();
    if (!meta.isReconcileRequired) {
      session.writeJournalMeta({ ...meta, isReconcileRequired: true });
    }
  }
  #reparentAndClearPendingRenameIntentInSession(session, localFileId) {
    const row = firstRow(
      session.readRows(
        [
          "select lf.normalized_path, pri.current_path from local_files lf",
          "left join pending_rename_intents pri on pri.local_file_id = lf.local_file_id",
          `where lf.local_file_id = ${sqlText(localFileId)};`
        ].join(" ")
      )
    );
    if (row === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const [rowPath, currentPath] = row;
    if (typeof rowPath !== "string" || currentPath !== null && typeof currentPath !== "string") {
      throw journalStoreError("journal_image_invalid");
    }
    const finalPath = currentPath ?? rowPath;
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText(localFileId)};`
    );
    session.exec(
      `delete from pending_rename_intents where local_file_id = ${sqlText(localFileId)};`
    );
    session.exec(
      [
        "update local_files set",
        `normalized_path = ${sqlText(finalPath)},`,
        "lifecycle_state = 'active',",
        "open_tombstone_id = null",
        `where local_file_id = ${sqlText(localFileId)};`
      ].join(" ")
    );
  }
  #recordLifecycleAttemptInSession(session, input) {
    session.exec(
      [
        "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
        "request_correlation_id) values (",
        `${sqlText(input.eventId)}, ${input.attemptedAtEpochMs},`,
        `${sqlText(input.outcomeLabel)}, ${sqlText(input.requestCorrelationId)});`
      ].join(" ")
    );
    session.exec(
      [
        "delete from journal_attempts where event_id =",
        `${sqlText(input.eventId)} and attempt_ordinal not in (`,
        "select attempt_ordinal from journal_attempts",
        `where event_id = ${sqlText(input.eventId)}`,
        `order by attempt_ordinal desc limit ${MAX_EVENT_ATTEMPT_HISTORY});`
      ].join(" ")
    );
  }
  /**
   * Session-scoped lifecycle-event writer. Lets the atomic writer
   * ({@link recordLifecycleEventWithFreeze}) chain the freeze + event +
   * path-rebind inside one transaction.
   */
  #recordLifecycleEventInSession(session, options) {
    validateOptions(options.operands, {
      localFile: options.localFile,
      tombstoneId: options.tombstoneId
    });
    const tombstoneId = options.tombstoneId;
    const lifecycleState = initialStateFor(
      options.operands.operation,
      void 0
    );
    const read = (sql) => session.readRows(sql);
    const replay = this.#findReplay(read, options.localFile.localFileId, options.operands);
    if (replay !== null) {
      return replay;
    }
    if (options.operands.predecessorEventId !== null) {
      const predecessor = firstRow(
        read(
          `select event_id from journal_events where event_id = ${sqlText(options.operands.predecessorEventId)};`
        )
      );
      if (predecessor === null) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    const eventId = this.#createId();
    const idempotencyKey = this.#createId();
    const createdAt = this.#nowEpochMs();
    const isFrozen = options.operands.operation === "rename" || options.operands.operation === "move" || options.operands.operation === "delete" || options.operands.operation === "restore" ? 1 : 0;
    session.exec(
      [
        "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
        "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
        "safe_error, created_at_epoch_ms) values (",
        `${sqlText(eventId)}, ${sqlText(options.localFile.localFileId)},`,
        `${sqlText(idempotencyKey)},`,
        `${sqlText(options.operands.operation)},`,
        `${sqlText(LIFECYCLE_FINGERPRINT.sha256)},`,
        `${LIFECYCLE_FINGERPRINT.sizeBytes},`,
        `${sqlText(LIFECYCLE_FINGERPRINT.mediaType)},`,
        `'queued',`,
        `${isFrozen}, 0, null,`,
        `${createdAt});`
      ].join(" ")
    );
    session.exec(
      [
        "insert into lifecycle_event_operands (event_id, source_id, expected_version_id,",
        "expected_locator, target_locator, tombstone_id, policy_revision,",
        "predecessor_event_id) values (",
        `${sqlText(eventId)}, ${sqlText(options.operands.sourceId)},`,
        `${sqlText(options.operands.expectedVersionId)},`,
        `${options.operands.expectedLocator === null ? "null" : sqlText(options.operands.expectedLocator)},`,
        `${options.operands.targetLocator === null ? "null" : sqlText(options.operands.targetLocator)},`,
        `${tombstoneId === null ? "null" : sqlText(tombstoneId)},`,
        `${options.operands.policyRevision},`,
        `${options.operands.predecessorEventId === null ? "null" : sqlText(options.operands.predecessorEventId)});`
      ].join(" ")
    );
    const lastLocator = options.operands.targetLocator ?? options.operands.expectedLocator ?? null;
    const newTombstone = options.operands.operation === "delete" || options.operands.operation === "restore" ? tombstoneId : null;
    const newPath = options.operands.operation === "rename" || options.operands.operation === "move" ? options.newPath ?? options.operands.targetLocator ?? null : null;
    const observedOverride = options.operands.operation === "rename" || options.operands.operation === "move" ? {
      sha256: options.operands.capturedFingerprintSha256,
      sizeBytes: options.operands.capturedFingerprintSizeBytes,
      mediaType: options.operands.capturedFingerprintMediaType
    } : null;
    const observedWrite = observedOverride !== null && observedOverride.sha256 !== null && observedOverride.sizeBytes !== null && observedOverride.mediaType !== null ? [
      `observed_sha256 = ${sqlText(observedOverride.sha256)},`,
      `observed_size_bytes = ${observedOverride.sizeBytes},`,
      `observed_media_type = ${sqlText(observedOverride.mediaType)},`
    ].join(" ") : "";
    const pathWrite = newPath === null ? "" : `normalized_path = ${sqlText(newPath)},`;
    session.exec(
      [
        "update local_files set",
        `${pathWrite}`,
        `${observedWrite}`,
        `${lastLocator === null ? "last_locator = null," : `last_locator = ${sqlText(lastLocator)},`}`,
        `${newTombstone === null ? "open_tombstone_id = null," : `open_tombstone_id = ${sqlText(newTombstone)},`}`,
        `lifecycle_state = ${sqlText(lifecycleState)}`,
        `where local_file_id = ${sqlText(options.localFile.localFileId)};`
      ].join(" ")
    );
    if (options.forceFailureAfterExec) {
      throw journalStoreError("journal_mutation_failed");
    }
    const event = {
      eventId,
      localFileId: options.localFile.localFileId,
      idempotencyKey,
      operation: options.operands.operation,
      fingerprint: { ...LIFECYCLE_FINGERPRINT },
      state: "queued",
      attemptCount: 0,
      nextEligibleRetryEpochMs: null,
      safeError: null,
      operationId: null
    };
    return {
      event,
      eventId,
      eventIdempotencyKey: idempotencyKey,
      lifecycleState
    };
  }
  /**
   * Session-scoped variant of the freeze helper: every still-pending
   * content event (`queued` / `preflight` / `waiting_retry`) of the
   * tracked file flips to terminal `deferred_lifecycle` inside the
   * SAME transaction the lifecycle event lands in.
   */
  #freezePendingForLocalFileInSession(session, localFileId) {
    if (!isUuid(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const existing = firstRow(
      session.readRows(
        `select local_file_id from local_files where local_file_id = ${sqlText(localFileId)};`
      )
    );
    if (existing === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText(state)).join(", ");
    session.exec(
      [
        "update journal_events set",
        "state = 'deferred_lifecycle',",
        "next_eligible_retry_epoch_ms = null,",
        "safe_error = 'deferred_lifecycle',",
        "is_fingerprint_frozen = 1",
        `where local_file_id = ${sqlText(localFileId)}`,
        `and state in (${pendingStateList})`,
        "and operation in ('create', 'update');"
      ].join(" ")
    );
  }
  #findReplay(read, localFileId, operands) {
    const tombstoneFilter = operands.tombstoneId === null ? "is null" : `= ${sqlText(operands.tombstoneId)}`;
    const replayRow = firstRow(
      read(
        [
          `select je.event_id, je.idempotency_key, lf.lifecycle_state`,
          `from journal_events je`,
          `join local_files lf on lf.local_file_id = je.local_file_id`,
          `join lifecycle_event_operands leo on leo.event_id = je.event_id`,
          `where je.local_file_id = ${sqlText(localFileId)}`,
          `and je.operation = ${sqlText(operands.operation)}`,
          `and leo.source_id = ${sqlText(operands.sourceId)}`,
          `and leo.expected_version_id = ${sqlText(operands.expectedVersionId)}`,
          `and ${operands.expectedLocator === null ? "leo.expected_locator is null" : `leo.expected_locator = ${sqlText(operands.expectedLocator)}`}`,
          `and ${operands.targetLocator === null ? "leo.target_locator is null" : `leo.target_locator = ${sqlText(operands.targetLocator)}`}`,
          `and leo.tombstone_id ${tombstoneFilter}`,
          `and leo.policy_revision = ${operands.policyRevision}`,
          `and ${operands.predecessorEventId === null ? "leo.predecessor_event_id is null" : `leo.predecessor_event_id = ${sqlText(operands.predecessorEventId)}`}`,
          `order by je.created_at_epoch_ms desc, je.rowid desc`,
          `limit 1;`
        ].join(" ")
      )
    );
    if (replayRow === null) {
      return null;
    }
    const [eventId, idempotencyKey, lifecycleState] = replayRow;
    if (typeof eventId !== "string" || typeof idempotencyKey !== "string" || typeof lifecycleState !== "string" || !isLifecycleLocalFileState(lifecycleState)) {
      throw journalStoreError("journal_image_invalid");
    }
    return {
      eventId,
      eventIdempotencyKey: idempotencyKey,
      lifecycleState,
      event: {
        eventId,
        localFileId,
        idempotencyKey,
        operation: operands.operation,
        fingerprint: { ...LIFECYCLE_FINGERPRINT },
        state: "queued",
        attemptCount: 0,
        nextEligibleRetryEpochMs: null,
        safeError: null,
        operationId: null
      }
    };
  }
};

// src/journal/lifecycle-capture.ts
var SETTLE_DEFERRAL_ATTEMPTS = 40;
var SETTLE_DEFERRED = /* @__PURE__ */ Symbol("settle-deferred");
function isPositiveInteger3(value) {
  return Number.isInteger(value) && value > 0;
}
function parseReason(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string") {
      return reason;
    }
  }
  return null;
}
function isStoreError2(error) {
  return parseReason(error) !== null;
}
function parentOfPath(path) {
  const lastSlash = path.lastIndexOf("/");
  return lastSlash === -1 ? "" : path.slice(0, lastSlash);
}
function isUuid2(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}
var LifecycleCaptureImpl = class {
  #repository;
  #lifecycle;
  #vaultReader;
  #nowEpochMs;
  #createId;
  #policyRevision;
  #settleDelayMs;
  #settleDeferralAttempts;
  #failureReporter;
  #echoSuppressor;
  #settleTimers = /* @__PURE__ */ new Map();
  #settleWaiters = /* @__PURE__ */ new Map();
  #pendingRenameTimers = /* @__PURE__ */ new Map();
  #pendingRenameWaiters = /* @__PURE__ */ new Map();
  #pendingRenameDeferralBudget = /* @__PURE__ */ new Map();
  #ownerBoundRenamePredecessors = /* @__PURE__ */ new Map();
  #pendingRenameMutationTails = /* @__PURE__ */ new Map();
  #deleteDeferralTimers = /* @__PURE__ */ new Map();
  #isDisposed = false;
  constructor(options) {
    if (!isPositiveInteger3(options.policyRevision)) {
      throw new TypeError("invalid policy revision");
    }
    if (options.settleDelayMs !== void 0 && !isPositiveInteger3(options.settleDelayMs)) {
      throw new TypeError("invalid settle delay");
    }
    if (options.settleDeferralAttempts !== void 0 && !isPositiveInteger3(options.settleDeferralAttempts)) {
      throw new TypeError("invalid settle deferral attempts");
    }
    this.#repository = options.repository;
    this.#lifecycle = options.lifecycle;
    this.#vaultReader = typeof options.vaultReader === "function" ? options.vaultReader() : options.vaultReader;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#policyRevision = options.policyRevision;
    this.#settleDelayMs = options.settleDelayMs ?? FILE_SETTLE_DELAY_MS;
    this.#settleDeferralAttempts = options.settleDeferralAttempts ?? SETTLE_DEFERRAL_ATTEMPTS;
    this.#failureReporter = options.failureReporter ?? null;
    this.#echoSuppressor = options.echoSuppressor ?? null;
  }
  /**
   * Observe one Vault rename notification. The `priorPath` is the
   * pre-rename locator (the durable row still references it); `file.path`
   * is the new locator and `file.parent.path` decides between `rename`
   * (same parent) and `move` (different parent). The per-path settle
   * delay is applied before the durable record so a burst of rapid
   * rename notifications collapses into one event.
   *
   * An untracked prior path resolves to `null` (no event minted); a
   * missing local source identity resolves to `null` after durably
   * flagging `reconcile_required` (fail-closed). A thrown journal
   * store error propagates to the caller.
   *
   * After the settle completes, the target file bytes are read and
   * fingerprinted; the lifecycle event and the `local_files` path
   * rebind land in one transaction (spec 7.1 fix round 1 I1 + I2).
   */
  captureRename(file, priorPath, context) {
    if (this.#isDisposed) {
      return Promise.resolve(null);
    }
    const normalizedPrior = this.#normalizePathOrNull(priorPath);
    const normalizedNew = this.#normalizePathOrNull(file.path);
    if (normalizedPrior === null || normalizedNew === null) {
      return Promise.resolve(null);
    }
    let intentOwner;
    try {
      intentOwner = this.#resolvePendingRenameOwner(normalizedPrior);
    } catch (error) {
      if (isStoreError2(error)) {
        return Promise.reject(journalStoreError("journal_mutation_failed"));
      }
      return Promise.reject(journalStoreError("journal_mutation_failed"));
    }
    if (intentOwner !== null) {
      context?.onOwnerResolved?.(intentOwner.localFile.localFileId);
    }
    if (intentOwner !== null && (intentOwner.hasPendingIntent || intentOwner.localFile.sourceId === null && intentOwner.localFile.baseVersionId === null && this.#hasInFlightEvent(intentOwner.localFile.localFileId))) {
      return this.#capturePendingRenameObservation(
        intentOwner.localFile.localFileId,
        normalizedPrior,
        normalizedNew
      );
    }
    const operation = this.#renameOperation(normalizedPrior, normalizedNew);
    const settleKey = `${operation}:${normalizedPrior}->${normalizedNew}`;
    return new Promise((resolve, reject) => {
      let deferralBudget = this.#settleDeferralAttempts;
      const armSettleTimer = () => {
        this.#settleTimers.set(
          settleKey,
          setTimeout(() => {
            this.#settleTimers.delete(settleKey);
            const pending = this.#settleWaiters.get(settleKey);
            this.#settleWaiters.delete(settleKey);
            if (pending === void 0) {
              return;
            }
            for (const run of pending) {
              run();
            }
          }, this.#settleDelayMs)
        );
      };
      const settleFailed = (error) => {
        if (isStoreError2(error)) {
          reject(error);
        } else {
          reject(journalStoreError("journal_mutation_failed"));
        }
      };
      const attempt = () => {
        if (this.#isDisposed) {
          resolve(null);
          return;
        }
        this.#commitRenameWithRebind(operation, normalizedPrior, normalizedNew).then(
          (result) => {
            if (result !== SETTLE_DEFERRED) {
              resolve(result);
              return;
            }
            if (deferralBudget <= 0) {
              void this.#flagReconcileRequiredOrReport().then(
                () => resolve(null),
                settleFailed
              );
              return;
            }
            deferralBudget -= 1;
            const rePending = this.#settleWaiters.get(settleKey) ?? /* @__PURE__ */ new Set();
            rePending.add(attempt);
            this.#settleWaiters.set(settleKey, rePending);
            armSettleTimer();
          },
          settleFailed
        );
      };
      const waiters = this.#settleWaiters.get(settleKey) ?? /* @__PURE__ */ new Set();
      waiters.add(attempt);
      this.#settleWaiters.set(settleKey, waiters);
      const running = this.#settleTimers.get(settleKey);
      if (running !== void 0) {
        clearTimeout(running);
      }
      armSettleTimer();
    });
  }
  /**
   * Resolve a watcher edge only through its durable owner proof: the local
   * row at the observed prior endpoint, or the one intent whose current
   * endpoint equals that prior. A bare path miss never manufactures an
   * owner, which preserves the provenance boundary for rapid A -> B -> C.
   */
  #resolvePendingRenameOwner(normalizedPrior) {
    const direct = this.#repository.readLocalFileByPath(normalizedPrior);
    if (direct !== null) {
      return {
        localFile: direct,
        hasPendingIntent: this.#readPendingRenameIntentForLocalFileOrReport(direct.localFileId) !== null
      };
    }
    const intent = this.#readPendingRenameIntentByCurrentPathOrReport(normalizedPrior);
    if (intent !== null) {
      const owner2 = this.#repository.readLocalFileByLocalFileId(intent.localFileId);
      if (owner2 === null) {
        return null;
      }
      return { localFile: owner2, hasPendingIntent: true };
    }
    const predecessor = this.#ownerBoundRenamePredecessors.get(normalizedPrior);
    if (predecessor === void 0) {
      return null;
    }
    const owner = this.#repository.readLocalFileByLocalFileId(predecessor.localFileId);
    if (owner === null || owner.normalizedPath !== predecessor.ownedRowPath) {
      return null;
    }
    return { localFile: owner, hasPendingIntent: true };
  }
  /** Persist the owned edge before any settle delay, then coalesce by owner. */
  async #capturePendingRenameObservation(localFileId, observedPriorPath, observedCurrentPath) {
    if (this.#readLifecycleState(localFileId) === "restore_pending") {
      return null;
    }
    const owner = this.#repository.readLocalFileByLocalFileId(localFileId);
    if (owner === null) {
      return null;
    }
    const observationToken = /* @__PURE__ */ Symbol("owner-bound-rename-observation");
    this.#ownerBoundRenamePredecessors.set(observedCurrentPath, {
      localFileId,
      ownedRowPath: owner.normalizedPath,
      observationToken
    });
    const previousMutation = this.#pendingRenameMutationTails.get(localFileId);
    const mutation = (previousMutation ?? Promise.resolve()).then(async () => {
      await this.#lifecycle.recordOrComposePendingRenameIntent({
        localFileId,
        observedPriorPath,
        observedCurrentPath
      });
    });
    this.#pendingRenameMutationTails.set(localFileId, mutation);
    try {
      await mutation;
    } catch (error) {
      if (error instanceof PendingRenameIntentConflictError) {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_conflict");
        return null;
      }
      this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      if (isStoreError2(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    } finally {
      if (this.#pendingRenameMutationTails.get(localFileId) === mutation) {
        this.#pendingRenameMutationTails.delete(localFileId);
      }
      const predecessor = this.#ownerBoundRenamePredecessors.get(observedCurrentPath);
      if (predecessor?.observationToken === observationToken) {
        this.#ownerBoundRenamePredecessors.delete(observedCurrentPath);
      }
    }
    return this.#schedulePendingRenameMaterialization(localFileId);
  }
  /** Arm one owner-scoped timer; each new linked observation resets it. */
  #schedulePendingRenameMaterialization(localFileId) {
    return new Promise((resolve, reject) => {
      const waiters = this.#pendingRenameWaiters.get(localFileId) ?? /* @__PURE__ */ new Set();
      waiters.add({ resolve, reject });
      this.#pendingRenameWaiters.set(localFileId, waiters);
      const previousTimer = this.#pendingRenameTimers.get(localFileId);
      if (previousTimer !== void 0) {
        clearTimeout(previousTimer);
      }
      if (!this.#pendingRenameDeferralBudget.has(localFileId)) {
        this.#pendingRenameDeferralBudget.set(localFileId, this.#settleDeferralAttempts);
      }
      this.#pendingRenameTimers.set(
        localFileId,
        setTimeout(() => {
          this.#pendingRenameTimers.delete(localFileId);
          void this.#settlePendingRenameIntent(localFileId);
        }, this.#settleDelayMs)
      );
    });
  }
  /** Re-read current endpoints and materialize at most one immutable prefix. */
  async #settlePendingRenameIntent(localFileId) {
    const resolveAll = (result) => {
      const waiters = this.#pendingRenameWaiters.get(localFileId);
      this.#pendingRenameWaiters.delete(localFileId);
      this.#pendingRenameDeferralBudget.delete(localFileId);
      for (const waiter of waiters ?? []) {
        waiter.resolve(result);
      }
    };
    const rejectAll = (error) => {
      const waiters = this.#pendingRenameWaiters.get(localFileId);
      this.#pendingRenameWaiters.delete(localFileId);
      this.#pendingRenameDeferralBudget.delete(localFileId);
      for (const waiter of waiters ?? []) {
        waiter.reject(error);
      }
    };
    if (this.#isDisposed) {
      resolveAll(null);
      return;
    }
    try {
      let intent;
      try {
        intent = this.#lifecycle.readPendingRenameIntentForLocalFile(localFileId);
      } catch (error) {
        this.#reportPendingRenameIntentReadFailure();
        rejectAll(isStoreError2(error) ? error : journalStoreError("journal_query_failed"));
        return;
      }
      if (intent === null) {
        resolveAll(null);
        return;
      }
      const localFile = this.#repository.readLocalFileByLocalFileId(localFileId);
      if (localFile === null || this.#readLifecycleState(localFileId) === "restore_pending") {
        resolveAll(null);
        return;
      }
      if (localFile.sourceId === null || localFile.baseVersionId === null) {
        if (this.#hasInFlightEvent(localFileId)) {
          const remainingBudget = this.#pendingRenameDeferralBudget.get(localFileId) ?? 0;
          if (remainingBudget <= 0) {
            await this.#flagReconcileRequiredOrReport();
            resolveAll(null);
            return;
          }
          this.#pendingRenameDeferralBudget.set(localFileId, remainingBudget - 1);
          this.#pendingRenameTimers.set(
            localFileId,
            setTimeout(() => {
              this.#pendingRenameTimers.delete(localFileId);
              void this.#settlePendingRenameIntent(localFileId);
            }, this.#settleDelayMs)
          );
          return;
        }
        if (this.#isUncommittedTransitRow(localFileId)) {
          await this.#lifecycle.reparentAndClearPendingRenameIntent(localFileId);
          resolveAll(null);
          return;
        }
        await this.#flagReconcileRequiredOrReport();
        resolveAll(null);
        return;
      }
      const targetBytes = await this.#vaultReader.readRegularFileBytes(intent.currentPath);
      if (targetBytes === null) {
        resolveAll(null);
        return;
      }
      const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
      if (this.#echoSuppressor !== null) {
        const observation = {
          priorLocator: intent.priorPath,
          targetLocator: intent.currentPath,
          sourceId: localFile.sourceId,
          fingerprint: targetFingerprint
        };
        const echoSuppressor = this.#echoSuppressor;
        const consumed = await this.#lifecycle.consumePendingRenameEchoAndReparent(
          localFileId,
          (session) => echoSuppressor.consumeRenameObservationInSession(session, observation)
        );
        if (consumed) {
          resolveAll(null);
          return;
        }
      }
      const prefix = await this.#lifecycle.recordPendingRenameLifecycleEvent(
        localFileId,
        targetFingerprint
      );
      if (prefix === null) {
        resolveAll(null);
        return;
      }
      const operands = this.#lifecycle.readLifecycleOperands(prefix.eventId);
      if (operands === null || operands.operation !== "rename" && operands.operation !== "move") {
        await this.#flagReconcileRequiredOrReport();
        resolveAll(null);
        return;
      }
      resolveAll({
        operation: operands.operation,
        localFileId,
        eventId: prefix.eventId,
        predecessorEventId: null,
        capturedFingerprintSha256: targetFingerprint.sha256,
        capturedFingerprintSizeBytes: targetFingerprint.sizeBytes
      });
    } catch (error) {
      if (error instanceof PendingRenameIntentConflictError) {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_conflict");
      } else {
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      }
      rejectAll(isStoreError2(error) ? error : journalStoreError("journal_mutation_failed"));
    }
  }
  #readPendingRenameIntentForLocalFileOrReport(localFileId) {
    try {
      return this.#lifecycle.readPendingRenameIntentForLocalFile(localFileId);
    } catch (error) {
      this.#reportPendingRenameIntentReadFailure();
      throw error;
    }
  }
  #readPendingRenameIntentByCurrentPathOrReport(normalizedPath) {
    try {
      return this.#lifecycle.readPendingRenameIntentByCurrentPath(normalizedPath);
    } catch (error) {
      this.#reportPendingRenameIntentReadFailure();
      throw error;
    }
  }
  #reportPendingRenameIntentReadFailure() {
    this.#failureReporter?.reportJournalFailure("pending_rename_intent_read_failed");
  }
  /**
   * Observe one Vault delete notification. An untracked path is a
   * quiet no-op (no lifecycle event minted); a tracked path freezes
   * any pending content work, persists a delete lifecycle event in the
   * same transaction the lifecycle repository already owns, and marks
   * the local mapping as `tombstoned` so the explicit restore surface
   * can reach it. The `tombstoneId` is minted from the same identity
   * seam the lifecycle repository uses.
   */
  async captureDelete(file, tombstoneId) {
    if (this.#isDisposed) {
      return null;
    }
    const normalizedPath = this.#normalizePathOrNull(file.path);
    if (normalizedPath === null) {
      return null;
    }
    let localFile = this.#repository.readLocalFileByPath(normalizedPath);
    if (localFile === null) {
      const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(
        normalizedPath
      );
      if (pendingIntent !== null) {
        localFile = this.#repository.readLocalFileByLocalFileId(pendingIntent.localFileId);
        if (localFile !== null) {
          this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
        }
        return null;
      }
    } else {
      const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(normalizedPath);
      if (pendingIntent !== null && pendingIntent.localFileId === localFile.localFileId) {
        this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
        return null;
      }
    }
    if (localFile === null) {
      return null;
    }
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      if (this.#isUncommittedTransitRow(localFile.localFileId)) {
        await this.#repository.removeLocalMapping(localFile.localFileId);
        return null;
      }
      if (this.#hasInFlightEvent(localFile.localFileId)) {
        this.#scheduleDeleteDeferralRetry(normalizedPath, tombstoneId);
        return null;
      }
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    const issuedTombstoneId = tombstoneId ?? this.#createId();
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "delete",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: normalizedPath,
        targetLocator: null,
        tombstoneId: issuedTombstoneId,
        predecessorEventId: null
      }),
      localFile,
      tombstoneId: issuedTombstoneId
    });
    return {
      localFileId: localFile.localFileId,
      tombstoneId: issuedTombstoneId,
      eventId: result.eventId
    };
  }
  /**
   * Reserve one explicit-restore target locator (the reservation-first
   * protocol): the durable reservation lands the moment the restore
   * command accepts the target path, BEFORE any bytes are staged, so the
   * convergence lane can never ship the staged restore bytes as a fresh
   * source at the target. Delegates to
   * {@link LifecycleRepository.reserveRestoreTarget}; refusals come back
   * as the closed result shape (never a throw) and a persistence failure
   * rethrows the closed store reason after one
   * `restore_reservation_persist_failed` trail token.
   */
  async reserveRestoreTarget(localFileId, targetPath) {
    if (this.#isDisposed || !isUuid2(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const normalizedTarget = this.#normalizePathOrNull(targetPath);
    if (normalizedTarget === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    try {
      return await this.#lifecycle.reserveRestoreTarget(localFileId, normalizedTarget);
    } catch (error) {
      this.#failureReporter?.reportJournalFailure("restore_reservation_persist_failed");
      if (isStoreError2(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    }
  }
  /**
   * Release one explicit-restore reservation (the restore command's
   * explicit Cancel path): the row returns to its pre-reservation path
   * and `tombstoned` state. Modal dismissal never releases — a dangling
   * reservation stays durable and resumable through the picker.
   */
  async releaseRestoreTarget(localFileId) {
    if (!isUuid2(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    try {
      await this.#lifecycle.releaseRestoreTarget(localFileId);
    } catch (error) {
      if (isStoreError2(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    }
  }
  /**
   * The user-driven restore surface (confirm step of the
   * reservation-first protocol): the row must already be reserved —
   * `restore_pending` and rebound to the target path by
   * {@link reserveRestoreTarget} — before this method runs. The adapter
   * verifies the target path's bytes still hash to the file's last
   * committed content hash; a mismatch rejects with
   * `journal_mutation_failed` and the reservation stays resumable. The
   * tombstone is NEVER consumed here: only the committed receipt
   * advances the row past `restore_pending`.
   */
  async requestRestore(localFileId, targetPath) {
    if (!isUuid2(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    const normalizedTarget = this.#normalizePathOrNull(targetPath);
    if (normalizedTarget === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const localFile = this.#repository.readLocalFileByPath(normalizedTarget);
    if (localFile === null || localFile.localFileId !== localFileId || localFile.sourceId === null || localFile.baseVersionId === null || localFile.lastCommittedFingerprint === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (this.#readLifecycleState(localFileId) !== "restore_pending") {
      throw journalStoreError("journal_mutation_failed");
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFileId);
    if (openTombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetBytes = await this.#vaultReader.readRegularFileBytes(normalizedTarget);
    if (targetBytes === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (targetFingerprint.sha256 !== localFile.lastCommittedFingerprint.sha256 || targetFingerprint.sizeBytes !== localFile.lastCommittedFingerprint.sizeBytes) {
      throw journalStoreError("journal_mutation_failed");
    }
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "restore",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: null,
        targetLocator: normalizedTarget,
        tombstoneId: openTombstoneId,
        predecessorEventId
      }),
      localFile,
      tombstoneId: openTombstoneId
    });
    return {
      operation: "restore",
      localFileId,
      eventId: result.eventId,
      predecessorEventId
    };
  }
  /**
   * The automatic restore detector: when a Vault create/modify event
   * re-uses a path whose local mapping is tombstoned, the capture path
   * calls this before recording a fresh `create`. The detector rejects
   * (with `journal_mutation_failed`) unless BOTH conditions hold:
   *
   *   1. the `local_files` row is still mapped to a retained source id;
   *   2. the bytes at the path hash to the file's LAST COMMITTED
   *      fingerprint (never the mutable observed fingerprint).
   *
   * On success the adapter records a `restore` event in one
   * transaction and consumes the tombstone via
   * {@link LifecycleRepository.consumeRestoreSuccessor}.
   *
   * Class-only helper (not on the {@link LifecycleCapture} port): the
   * capture composition uses it to detect automatic restores before
   * minting a fresh create.
   */
  async detectAutomaticRestore(normalizedPath) {
    if (this.#isDisposed) {
      throw journalStoreError("journal_mutation_failed");
    }
    const cleanedPath = this.#normalizePathOrNull(normalizedPath);
    if (cleanedPath === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const localFile = this.#repository.readLocalFileByPath(cleanedPath);
    if (localFile === null || localFile.sourceId === null || localFile.baseVersionId === null || localFile.lastCommittedFingerprint === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
      throw journalStoreError("journal_mutation_failed");
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFile.localFileId);
    if (openTombstoneId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetBytes = await this.#vaultReader.readRegularFileBytes(cleanedPath);
    if (targetBytes === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (targetFingerprint.sha256 !== localFile.lastCommittedFingerprint.sha256 || targetFingerprint.sizeBytes !== localFile.lastCommittedFingerprint.sizeBytes) {
      throw journalStoreError("journal_mutation_failed");
    }
    const predecessorEventId = this.#readPredecessorDeleteEventId(localFile.localFileId);
    if (predecessorEventId === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation: "restore",
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: null,
        targetLocator: cleanedPath,
        tombstoneId: openTombstoneId,
        predecessorEventId
      }),
      localFile,
      tombstoneId: openTombstoneId
    });
    return {
      operation: "restore",
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId
    };
  }
  /**
   * Fail-closed reconcile flag: a tombstoned path that re-appeared with
   * bytes that do NOT match the last-committed fingerprint must NOT be
   * re-captured as a fresh create. Instead the lifecycle capture
   * durably flags the row as `reconcile_required`, drops the open
   * tombstone so the file is not eligible for automatic restore, and
   * returns `true`. The capture composition then refuses the create /
   * update admission (spec 7.1 fix round 1 C2).
   */
  async markTombstonedPathReconcileRequired(normalizedPath) {
    const cleanedPath = this.#normalizePathOrNull(normalizedPath);
    if (cleanedPath === null) {
      return false;
    }
    const localFile = this.#repository.readLocalFileByPath(cleanedPath);
    if (localFile === null) {
      return false;
    }
    const openTombstoneId = this.#readOpenTombstoneId(localFile.localFileId);
    if (openTombstoneId === null) {
      return false;
    }
    await this.#lifecycle.recordLifecycleReconcileForLocalFile(localFile.localFileId);
    return true;
  }
  /**
   * Re-arm every durable chain after journal recovery before automatic
   * snapshot admission or outbound dispatch. Enumeration failure is surfaced
   * as a closed read token and propagates so composition can fail closed.
   */
  async resumePendingRenameIntents() {
    let intents;
    try {
      intents = this.#lifecycle.readPendingRenameIntents();
    } catch (error) {
      this.#failureReporter?.reportJournalFailure("pending_rename_intent_read_failed");
      if (isStoreError2(error)) {
        throw error;
      }
      throw journalStoreError("journal_query_failed");
    }
    for (const intent of intents) {
      void this.#schedulePendingRenameMaterialization(intent.localFileId).catch((error) => {
        if (isStoreError2(error)) {
          return;
        }
        this.#failureReporter?.reportJournalFailure("pending_rename_intent_persist_failed");
      });
    }
  }
  /** Re-arm one rebased successor after its immutable prefix receipt commits. */
  rearmPendingRenameIntent(localFileId) {
    if (this.#isDisposed || !isUuid2(localFileId)) {
      return;
    }
    void this.#schedulePendingRenameMaterialization(localFileId).catch(() => void 0);
  }
  /** Settle all queued rename observations and stop accepting new ones. */
  dispose() {
    this.#isDisposed = true;
    for (const timer of this.#settleTimers.values()) {
      clearTimeout(timer);
    }
    this.#settleTimers.clear();
    for (const timer of this.#deleteDeferralTimers.values()) {
      clearTimeout(timer);
    }
    this.#deleteDeferralTimers.clear();
    for (const [, waiters] of this.#settleWaiters) {
      for (const resolve of waiters) {
        resolve();
      }
    }
    this.#settleWaiters.clear();
    for (const timer of this.#pendingRenameTimers.values()) {
      clearTimeout(timer);
    }
    this.#pendingRenameTimers.clear();
    for (const [, waiters] of this.#pendingRenameWaiters) {
      for (const waiter of waiters) {
        waiter.resolve(null);
      }
    }
    this.#pendingRenameWaiters.clear();
    this.#pendingRenameDeferralBudget.clear();
    this.#ownerBoundRenamePredecessors.clear();
    this.#pendingRenameMutationTails.clear();
  }
  /**
   * Retry one delete observation whose row was mid-create-flight, bounded
   * by {@link LifecycleCaptureOptions.settleDeferralAttempts}: each retry
   * waits one settle delay and re-runs the whole delete classification
   * (identity may have landed, the create may have terminalized failed —
   * the transit heal then owns the row — or the budget exhausts and the
   * fail-closed reconcile flag keeps its meaning for genuine pathology).
   */
  #scheduleDeleteDeferralRetry(normalizedPath, tombstoneId) {
    const running = this.#deleteDeferralTimers.get(normalizedPath);
    if (running !== void 0) {
      return;
    }
    const attempts = { remaining: this.#settleDeferralAttempts };
    const timer = setTimeout(() => {
      this.#deleteDeferralTimers.delete(normalizedPath);
      void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
    }, this.#settleDelayMs);
    this.#deleteDeferralTimers.set(normalizedPath, timer);
  }
  async #retryDeferredDelete(normalizedPath, tombstoneId, attempts) {
    if (this.#isDisposed) {
      return;
    }
    let localFile = this.#repository.readLocalFileByPath(normalizedPath);
    const pendingIntent = this.#lifecycle.readPendingRenameIntentOwningEndpoint(normalizedPath);
    if (pendingIntent !== null && (localFile === null || pendingIntent.localFileId === localFile.localFileId)) {
      localFile = this.#repository.readLocalFileByLocalFileId(pendingIntent.localFileId);
      if (localFile === null) {
        return;
      }
      if (attempts.remaining <= 0) {
        await this.#flagReconcileRequiredOrReport().catch(() => void 0);
        return;
      }
      attempts.remaining -= 1;
      const timer2 = setTimeout(() => {
        this.#deleteDeferralTimers.delete(normalizedPath);
        void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
      }, this.#settleDelayMs);
      this.#deleteDeferralTimers.set(normalizedPath, timer2);
      return;
    }
    if (localFile === null) {
      return;
    }
    if (localFile.sourceId !== null && localFile.baseVersionId !== null) {
      await this.captureDelete(
        { path: normalizedPath, parent: null },
        tombstoneId
      ).catch(() => void 0);
      return;
    }
    if (this.#isUncommittedTransitRow(localFile.localFileId)) {
      await this.#repository.removeLocalMapping(localFile.localFileId).catch(() => void 0);
      return;
    }
    if (!this.#hasInFlightEvent(localFile.localFileId)) {
      await this.#flagReconcileRequiredOrReport().catch(() => void 0);
      return;
    }
    if (attempts.remaining <= 0) {
      await this.#flagReconcileRequiredOrReport().catch(() => void 0);
      return;
    }
    attempts.remaining -= 1;
    const timer = setTimeout(() => {
      this.#deleteDeferralTimers.delete(normalizedPath);
      void this.#retryDeferredDelete(normalizedPath, tombstoneId, attempts);
    }, this.#settleDelayMs);
    this.#deleteDeferralTimers.set(normalizedPath, timer);
  }
  // --- internals ---------------------------------------------------------------------------
  /**
   * Whether one tracked row is an uncommitted transit mapping: no source
   * identity, nothing in flight (`queued` / `preflight` / `uploading` /
   * `waiting_retry`) and nothing ever committed — a phantom whose only
   * history is dead terminal events (typically a create that closed
   * `blocked_conflict` on the vault's untitled-transit name). Such a row
   * carries no canonical evidence, so a rename or delete of it is
   * operator transit action, not corruption: the mapping is quietly
   * removed and the file re-admits fresh at its real name. A row with
   * live in-flight work or any committed history keeps the fail-closed
   * `reconcile_required` rule (an upload may still commit server-side).
   */
  #isUncommittedTransitRow(localFileId) {
    const events = this.#repository.readEventsByLocalFileId(localFileId);
    if (events.length === 0) {
      return true;
    }
    const pendingStates = new Set(JOURNAL_PENDING_EVENT_STATES);
    return events.every(
      (event) => !pendingStates.has(event.state) && event.state !== "committed" && event.state !== "no_change"
    );
  }
  /**
   * Whether any content event of the row is still in a pending (in-flight)
   * state — an upload whose outcome, and with it the row's identity, has
   * not landed yet. The settle-deferral branch of the rename/delete capture
   * keys on this: the row is neither droppable transit (work is live) nor
   * flaggable corruption (the outcome is merely pending).
   */
  #hasInFlightEvent(localFileId) {
    const pendingStates = new Set(JOURNAL_PENDING_EVENT_STATES);
    return this.#repository.readEventsByLocalFileId(localFileId).some((event) => pendingStates.has(event.state));
  }
  /** The rename operation token chosen by the parent-directory comparison. */
  #renameOperation(priorPath, newPath) {
    return parentOfPath(priorPath) === parentOfPath(newPath) ? "rename" : "move";
  }
  /**
   * Persist one rename or move lifecycle event AND rebind the
   * `local_files.normalized_path` to the new locator — both inside
   * the same transaction so a torn rename never leaves a row pointing
   * at the old path. After the per-path settle, the target file bytes
   * are fingerprinted and the fingerprint rides along on the operand.
   */
  async #commitRenameWithRebind(operation, priorPath, newPath) {
    let localFile;
    try {
      localFile = this.#repository.readLocalFileByPath(priorPath);
    } catch (error) {
      if (isStoreError2(error)) {
        throw journalStoreError("journal_mutation_failed");
      }
      throw error;
    }
    if (localFile === null) {
      return null;
    }
    if (this.#readLifecycleState(localFile.localFileId) === "restore_pending") {
      return null;
    }
    if (localFile.sourceId === null || localFile.baseVersionId === null) {
      if (this.#isUncommittedTransitRow(localFile.localFileId)) {
        await this.#repository.removeLocalMapping(localFile.localFileId);
        return null;
      }
      if (this.#hasInFlightEvent(localFile.localFileId)) {
        return SETTLE_DEFERRED;
      }
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    const targetBytes = await this.#vaultReader.readRegularFileBytes(newPath);
    if (targetBytes === null) {
      await this.#flagReconcileRequiredOrReport();
      return null;
    }
    const targetFingerprint = await deriveFrozenFingerprint(targetBytes);
    if (this.#echoSuppressor !== null) {
      const consumed = await this.#echoSuppressor.consumeRenameObservation({
        priorLocator: priorPath,
        targetLocator: newPath,
        sourceId: localFile.sourceId,
        fingerprint: targetFingerprint
      });
      if (consumed) {
        return null;
      }
    }
    const result = await this.#lifecycle.recordLifecycleEventWithFreeze({
      operands: this.#buildOperands({
        operation,
        sourceId: localFile.sourceId,
        expectedVersionId: localFile.baseVersionId,
        expectedLocator: priorPath,
        targetLocator: newPath,
        tombstoneId: null,
        predecessorEventId: null,
        capturedFingerprintSha256: targetFingerprint.sha256,
        capturedFingerprintSizeBytes: targetFingerprint.sizeBytes,
        capturedFingerprintMediaType: targetFingerprint.mediaType
      }),
      localFile,
      newPath
    });
    return {
      operation,
      localFileId: localFile.localFileId,
      eventId: result.eventId,
      predecessorEventId: null,
      capturedFingerprintSha256: targetFingerprint.sha256,
      capturedFingerprintSizeBytes: targetFingerprint.sizeBytes
    };
  }
  /** Build one validated operand record from the raw capture inputs. */
  #buildOperands(input) {
    return createLifecycleEventOperands({
      operation: input.operation,
      sourceId: input.sourceId,
      expectedVersionId: input.expectedVersionId,
      expectedLocator: input.expectedLocator,
      targetLocator: input.targetLocator,
      tombstoneId: input.tombstoneId,
      policyRevision: this.#policyRevision,
      predecessorEventId: input.predecessorEventId,
      capturedFingerprintSha256: input.capturedFingerprintSha256,
      capturedFingerprintSizeBytes: input.capturedFingerprintSizeBytes,
      capturedFingerprintMediaType: input.capturedFingerprintMediaType
    });
  }
  /** Normalize one Vault path to the canonical locator, or drop it closed. */
  #normalizePathOrNull(path) {
    if (typeof path !== "string") {
      return null;
    }
    try {
      return normalizePolicyLocator(path);
    } catch {
      return null;
    }
  }
  /** Read the open tombstone id of one tracked file from `local_files`. */
  #readOpenTombstoneId(localFileId) {
    try {
      const row = this.#repository.lifecycle.database.readAll(
        `select open_tombstone_id from local_files where local_file_id = '${localFileId}';`
      )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
  }
  /** Read the closed `lifecycle_state` of one tracked file, or null. */
  #readLifecycleState(localFileId) {
    try {
      const row = this.#repository.lifecycle.database.readAll(
        `select lifecycle_state from local_files where local_file_id = '${localFileId}';`
      )[0]?.values[0]?.[0];
      return typeof row === "string" && row.length > 0 ? row : null;
    } catch {
      return null;
    }
  }
  /** Read the most recent `delete` event id of one tracked file, or null. */
  #readPredecessorDeleteEventId(localFileId) {
    const events = this.#repository.readEventsByLocalFileId(localFileId);
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (event !== void 0 && event.operation === "delete") {
        return event.eventId;
      }
    }
    return null;
  }
  /** Set the journal `is_reconcile_required` flag durably (spec 6.4). */
  async #flagReconcileRequired() {
    await this.#repository.lifecycle.database.runSerializedMutation(async (session) => {
      const row = session.readRows(
        "select is_reconcile_required from journal_meta where singleton_key = 1;"
      )[0]?.values[0]?.[0];
      if (row === 0) {
        session.exec(
          "update journal_meta set is_reconcile_required = 1 where singleton_key = 1;"
        );
      }
    });
  }
  /** Surface a failed flag write, then reject so callers cannot treat it as settled. */
  async #flagReconcileRequiredOrReport() {
    await this.#flagReconcileRequired().catch((error) => {
      this.#failureReporter?.reportJournalFailure("lifecycle_reconcile_persist_failed");
      if (isStoreError2(error)) {
        throw error;
      }
      throw journalStoreError("journal_mutation_failed");
    });
  }
};

// src/device-sync/contracts.ts
var DEVICE_SYNC_SERVER_REASONS = [
  "device_cursor_gap",
  "device_cursor_regression",
  "device_cursor_ack_ahead",
  "device_event_unavailable",
  "device_event_integrity_failed",
  "device_manifest_not_found",
  "device_manifest_expired",
  "device_manifest_state_invalid",
  "device_manifest_page_invalid",
  "device_manifest_page_replay_mismatch",
  "device_manifest_digest_mismatch",
  "device_manifest_policy_advanced",
  "device_download_integrity_failed",
  "device_sync_dependency_unavailable"
];
var DEVICE_SYNC_ACTION_REASONS = [
  "device_manifest_identity_ambiguous",
  "device_manifest_local_diverged",
  "device_manifest_target_occupied",
  "device_manifest_action_stale",
  "device_manifest_policy_excluded"
];
var DEVICE_SYNC_TRANSPORT_REASONS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required"
];
var DEVICE_SYNC_LOCAL_REASONS = [
  "device_apply_trash_failed",
  "device_apply_vault_failed",
  "device_apply_recovery_abandoned",
  "device_apply_recovery_ambiguous",
  "device_manifest_capture_failed"
];
var DEVICE_SYNC_CURSOR_STAGES = ["pull", "acknowledge"];
var DEVICE_SYNC_APPLY_STAGES = [
  "prepare",
  "download",
  "verify_temp",
  "vault_mutation",
  "verify_final",
  "local_commit",
  "recovery",
  "trash"
];
var DEVICE_SYNC_RECONCILE_STAGES = [
  "start",
  "page",
  "finalize",
  "actions",
  "complete"
];
var DEVICE_SYNC_CREDENTIAL_STAGES = ["access_missing", "refresh_failed"];
var DEVICE_SYNC_COMPOSITION_READ_STAGES = [
  "status_read",
  "note_status_read",
  "retry_schedule_read",
  "sync_status_read"
];
var DEVICE_SYNC_EVENT_OPERATIONS = [
  "created",
  "updated",
  "renamed",
  "moved",
  "deleted",
  "restored"
];
var DEVICE_SYNC_REMOTE_APPLY_STATES = [
  "prepared",
  "temp_verified",
  "vault_mutated",
  "locally_applied",
  "server_acknowledged"
];
var MANIFEST_ACTION_KINDS = [
  "upload",
  "download",
  "apply_tombstone",
  "conflict",
  "no_change",
  "excluded"
];
var TERMINAL_DEVICE_EVENT_OUTCOMES = [
  "applied",
  "self_origin_no_op",
  "conflict",
  "tombstone_handled",
  "excluded"
];
var MANIFEST_ACTION_PROGRESS_OUTCOMES = ["received", "terminal_safe"];

// src/device-sync/schema.ts
var DEVICE_SYNC_REASON_TOKENS = [
  ...DEVICE_SYNC_SERVER_REASONS,
  ...DEVICE_SYNC_ACTION_REASONS,
  ...DEVICE_SYNC_TRANSPORT_REASONS,
  ...DEVICE_SYNC_LOCAL_REASONS,
  ...JOURNAL_STORE_ERROR_REASONS
];
function isDeviceSyncReason(value) {
  return typeof value === "string" && DEVICE_SYNC_REASON_TOKENS.includes(value);
}
function isDeviceEventOperation(value) {
  return typeof value === "string" && DEVICE_SYNC_EVENT_OPERATIONS.includes(value);
}
function isDeviceSyncRemoteApplyState(value) {
  return typeof value === "string" && DEVICE_SYNC_REMOTE_APPLY_STATES.includes(value);
}
var DEVICE_SYNC_STATE_COLUMNS = [
  "applied_sequence",
  "acknowledged_sequence",
  "observation_generation",
  "barrier_generation",
  "barrier_reason",
  "active_manifest_run_id",
  "manifest_checkpoint_sequence",
  "manifest_final_digest"
];
function firstRow2(result) {
  return result[0]?.values[0] ?? null;
}
function isNonNegativeInteger2(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function isPositiveInteger4(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}
function isNullableText2(value) {
  return value === null || typeof value === "string";
}
function isNullableNonNegativeInteger2(value) {
  return value === null || isNonNegativeInteger2(value);
}
function parseDeviceSyncStateRow(row) {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    appliedSequence,
    acknowledgedSequence,
    observationGeneration,
    barrierGeneration,
    barrierReason,
    activeManifestRunId,
    manifestCheckpointSequence,
    manifestFinalDigest
  ] = row;
  if (!isNonNegativeInteger2(appliedSequence) || !isNonNegativeInteger2(acknowledgedSequence) || acknowledgedSequence > appliedSequence || !isNonNegativeInteger2(observationGeneration) || !isNullableNonNegativeInteger2(barrierGeneration) || barrierReason !== null && !isDeviceSyncReason(barrierReason) || !isNullableText2(activeManifestRunId) || !isNullableNonNegativeInteger2(manifestCheckpointSequence) || !isNullableText2(manifestFinalDigest)) {
    throwImageInvalid();
  }
  return {
    appliedSequence,
    acknowledgedSequence,
    observationGeneration,
    barrierGeneration,
    barrierReason,
    activeManifestRunId,
    manifestCheckpointSequence,
    manifestFinalDigest
  };
}
function readDeviceSyncState(reader) {
  const row = firstRow2(
    reader.readAll(
      `select ${DEVICE_SYNC_STATE_COLUMNS.join(", ")} from device_sync_state where singleton_key = 1;`
    )
  );
  return parseDeviceSyncStateRow(row);
}
var REMOTE_APPLY_COLUMNS = [
  "event_sequence",
  "event_id",
  "source_id",
  "operation",
  "prior_locator",
  "target_locator",
  "base_sha256",
  "base_size_bytes",
  "base_media_type",
  "final_sha256",
  "final_size_bytes",
  "final_media_type",
  "temp_token",
  "rollback_token",
  "state",
  "safe_error_code"
];
function parseNullableFingerprint(sha256, sizeBytes, mediaType) {
  if (sha256 === null && sizeBytes === null && mediaType === null) {
    return null;
  }
  if (typeof sha256 !== "string" || !isNonNegativeInteger2(sizeBytes) || typeof mediaType !== "string") {
    throwImageInvalid();
  }
  return { sha256, sizeBytes, mediaType };
}
function parseRemoteApplyRow(row) {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    eventSequence,
    eventId,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    baseSha256,
    baseSizeBytes,
    baseMediaType,
    finalSha256,
    finalSizeBytes,
    finalMediaType,
    tempToken,
    rollbackToken,
    state,
    safeErrorCode
  ] = row;
  if (!isPositiveInteger4(eventSequence) || typeof eventId !== "string" || typeof sourceId !== "string" || !isDeviceEventOperation(operation) || !isNullableText2(priorLocator) || !isNullableText2(targetLocator) || !isNullableText2(tempToken) || !isNullableText2(rollbackToken) || !isDeviceSyncRemoteApplyState(state) || safeErrorCode !== null && !isDeviceSyncReason(safeErrorCode)) {
    throwImageInvalid();
  }
  return {
    eventSequence,
    eventId,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    baseFingerprint: parseNullableFingerprint(baseSha256, baseSizeBytes, baseMediaType),
    finalFingerprint: parseNullableFingerprint(finalSha256, finalSizeBytes, finalMediaType),
    tempToken,
    rollbackToken,
    state,
    safeErrorCode
  };
}
var REMOTE_APPLY_OPERATION_COLUMNS = [...REMOTE_APPLY_COLUMNS];
var ECHO_MARKER_COLUMN_LIST = [
  "event_sequence",
  "source_id",
  "operation",
  "prior_locator",
  "target_locator",
  "final_sha256",
  "final_size_bytes",
  "final_media_type"
];
function parseEchoMarkerRow(row) {
  if (row === null) {
    throwImageInvalid();
  }
  const [
    eventSequence,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    finalSha256,
    finalSizeBytes,
    finalMediaType
  ] = row;
  if (!isPositiveInteger4(eventSequence) || typeof sourceId !== "string" || !isDeviceEventOperation(operation) || !isNullableText2(priorLocator) || !isNullableText2(targetLocator)) {
    throwImageInvalid();
  }
  return {
    eventSequence,
    sourceId,
    operation,
    priorLocator,
    targetLocator,
    finalFingerprint: parseNullableFingerprint(finalSha256, finalSizeBytes, finalMediaType)
  };
}
var ECHO_MARKER_COLUMNS = [...ECHO_MARKER_COLUMN_LIST];
function throwImageInvalid() {
  throw journalStoreError("journal_image_invalid");
}

// src/device-sync/repository.ts
var UUID_PATTERN4 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
var MAX_OPAQUE_TOKEN_LENGTH = 256;
function isUuid3(value) {
  return typeof value === "string" && UUID_PATTERN4.test(value);
}
function isNonNegativeInteger3(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function isPositiveInteger5(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
}
function isSha256Hex(value) {
  return typeof value === "string" && SHA256_HEX_PATTERN.test(value);
}
function isClosedToken(value, closedSet) {
  return typeof value === "string" && closedSet.includes(value);
}
function isNullableLocator(value) {
  if (value === null) {
    return true;
  }
  if (typeof value !== "string" || value.length === 0) {
    return false;
  }
  return Array.from(value).every((character) => {
    const codeUnit = character.charCodeAt(0);
    return codeUnit >= 32 && codeUnit !== 127;
  });
}
function isNullableOpaqueToken(value) {
  if (value === null) {
    return true;
  }
  return isNullableLocator(value) && value.length <= MAX_OPAQUE_TOKEN_LENGTH;
}
function isNullableFingerprint(value) {
  if (value === null) {
    return true;
  }
  if (typeof value !== "object") {
    return false;
  }
  return isFrozenFingerprintShape(value);
}
function sqlText2(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
function sqlNullableText(value) {
  return value === null ? "null" : sqlText2(value);
}
function firstRow3(result) {
  return result[0]?.values[0] ?? null;
}
function isSameFingerprint(left, right) {
  if (left === null || right === null) {
    return left === right;
  }
  return left.sha256 === right.sha256 && left.sizeBytes === right.sizeBytes && left.mediaType === right.mediaType;
}
function isContentApplyOperation(operation) {
  return operation === "created" || operation === "updated";
}
function isLegalRemoteApplyTransition(operation, from, to) {
  const isContentApply = isContentApplyOperation(operation);
  switch (to) {
    case "prepared":
      return false;
    case "temp_verified":
      return from === "prepared" && isContentApply;
    case "vault_mutated":
      return from === "prepared" && !isContentApply || from === "temp_verified";
    case "locally_applied":
      return from === "vault_mutated" || from === "temp_verified" && isContentApply || from === "prepared" && !isContentApply;
    case "server_acknowledged":
      return from === "locally_applied";
  }
}
function validateRepairBarrierInput(input) {
  if (!isNonNegativeInteger3(input.generation) || !isDeviceSyncReason(input.reason)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateManifestPageReceipt(input) {
  if (!isUuid3(input.manifestRunId) || !isNonNegativeInteger3(input.pageNumber) || !isNonNegativeInteger3(input.entryCount) || !isSha256Hex(input.pageDigest) || !isNonNegativeInteger3(input.checkpointSequence) || input.finalDigest !== null && !isSha256Hex(input.finalDigest)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateManifestActionProgress(input) {
  if (!isUuid3(input.manifestRunId) || !isNonNegativeInteger3(input.actionIndex) || !isClosedToken(input.actionKind, MANIFEST_ACTION_KINDS) || !isClosedToken(input.outcome, MANIFEST_ACTION_PROGRESS_OUTCOMES) || input.reason !== null && !isDeviceSyncReason(input.reason)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validatePreparedRemoteApply(input) {
  if (!isPositiveInteger5(input.eventSequence) || !isUuid3(input.eventId) || !isUuid3(input.sourceId) || !isClosedToken(input.operation, DEVICE_SYNC_EVENT_OPERATIONS) || !isNullableLocator(input.priorLocator) || !isNullableLocator(input.targetLocator) || !isNullableFingerprint(input.baseFingerprint) || !isNullableFingerprint(input.finalFingerprint) || !isNullableOpaqueToken(input.tempToken) || !isNullableOpaqueToken(input.rollbackToken)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateRemoteApplyTransition(input) {
  if (!isPositiveInteger5(input.eventSequence) || !isClosedToken(input.state, DEVICE_SYNC_REMOTE_APPLY_STATES) || input.tempToken !== void 0 && !isNullableOpaqueToken(input.tempToken) || input.rollbackToken !== void 0 && !isNullableOpaqueToken(input.rollbackToken)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateTerminalDeviceEvent(input) {
  if (!isPositiveInteger5(input.eventSequence) || !isClosedToken(input.outcome, TERMINAL_DEVICE_EVENT_OUTCOMES) || input.reason !== null && !isDeviceSyncReason(input.reason)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateCompleteLocalRepair(input) {
  if (!isUuid3(input.manifestRunId) || !isNonNegativeInteger3(input.checkpointSequence) || !isNonNegativeInteger3(input.barrierGeneration)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
function validateEchoMarker(input) {
  if (!isPositiveInteger5(input.eventSequence) || !isUuid3(input.sourceId) || !isClosedToken(input.operation, DEVICE_SYNC_EVENT_OPERATIONS) || !isNullableLocator(input.priorLocator) || !isNullableLocator(input.targetLocator) || !isNullableFingerprint(input.finalFingerprint)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
var DeviceSyncRepository = class {
  #database;
  constructor(options) {
    this.#database = options.database;
  }
  /** The durable reconciliation state singleton (read-only). */
  readState() {
    return readDeviceSyncState(this.#database);
  }
  /**
   * Increment and return the next monotonic Vault observation generation
   * (spec 12.1). Observations continue under an active barrier and always
   * receive generations greater than the barrier's frozen one.
   */
  async nextObservationGeneration() {
    return this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      const nextGeneration = state.observationGeneration + 1;
      session.exec(
        `update device_sync_state set observation_generation = ${nextGeneration} where singleton_key = 1;`
      );
      return nextGeneration;
    });
  }
  /**
   * Start the one active repair barrier (spec 12.1): the barrier freezes
   * the CURRENT observation generation, so a second barrier, a run still
   * in progress, or a generation that is not current is refused.
   */
  async startRepairBarrier(input) {
    validateRepairBarrierInput(input);
    await this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (state.barrierGeneration !== null || state.activeManifestRunId !== null || input.generation !== state.observationGeneration) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set",
          `barrier_generation = ${input.generation},`,
          `barrier_reason = ${sqlText2(input.reason)}`,
          "where singleton_key = 1;"
        ].join(" ")
      );
    });
  }
  /**
   * Refine the ACTIVE repair barrier's closed reason after the reconciler
   * diagnosed one itself (the apply lattice outrunning the run checkpoint)
   * — the same durable verdict the applier's prepare-path gap already
   * persists, so the resting state stays readable through status and a
   * later resume's recovery branch can key on it. Refused when no barrier
   * is active (the verdict is meaningless without one).
   */
  async persistRepairBarrierReason(reason) {
    await this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set",
          `barrier_reason = ${sqlText2(reason)}`,
          "where singleton_key = 1;"
        ].join(" ")
      );
    });
  }
  /**
   * Advance the ACTIVE repair barrier to a fresh observation generation
   * (the 2026-09-03 restart-asymmetry fix): the next manifest start
   * carries the new generation, which the server answers by expiring the
   * device's unfinished run — the sanctioned invalidation of server-side
   * run evidence the client just contradicted. Returns the new barrier
   * generation. Refused when no barrier is active.
   */
  async advanceRepairBarrierGeneration(reason) {
    return this.#database.runSerializedMutation((session) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const nextGeneration = state.observationGeneration + 1;
      session.exec(
        [
          "update device_sync_state set",
          `observation_generation = ${nextGeneration},`,
          `barrier_generation = ${nextGeneration},`,
          `barrier_reason = ${sqlText2(reason)}`,
          "where singleton_key = 1;"
        ].join(" ")
      );
      return nextGeneration;
    });
  }
  /**
   * Record one accepted manifest page receipt (spec 7.3, 12.1): the run
   * and its checkpoint bind with the first accepted page, pages land in
   * exact contiguous order, and a replayed page number must carry the
   * exact same evidence.
   */
  async recordManifestPage(input) {
    validateManifestPageReceipt(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null) {
        return block("device_manifest_state_invalid");
      }
      if (state.activeManifestRunId === null) {
        if (input.pageNumber !== 0) {
          return block("device_manifest_page_invalid");
        }
      } else {
        if (input.manifestRunId !== state.activeManifestRunId) {
          return block("device_manifest_state_invalid");
        }
        if (state.manifestCheckpointSequence !== null && input.checkpointSequence !== state.manifestCheckpointSequence) {
          return block("device_manifest_state_invalid");
        }
      }
      if (input.finalDigest !== null) {
        if (state.manifestFinalDigest === null) {
          session.exec(
            `update device_sync_state set manifest_final_digest = ${sqlText2(input.finalDigest)} where singleton_key = 1;`
          );
        } else if (state.manifestFinalDigest !== input.finalDigest) {
          return block("device_manifest_digest_mismatch");
        }
      }
      const existingPage = firstRow3(
        session.readRows(
          [
            "select entry_count, page_digest from manifest_page_progress",
            `where manifest_run_id = ${sqlText2(input.manifestRunId)}`,
            `and page_number = ${input.pageNumber};`
          ].join(" ")
        )
      );
      if (existingPage !== null) {
        const [entryCount, pageDigest] = existingPage;
        if (entryCount !== input.entryCount || pageDigest !== input.pageDigest) {
          return block("device_manifest_page_replay_mismatch");
        }
        return;
      }
      const maxPageRow = firstRow3(
        session.readRows(
          [
            "select max(page_number) from manifest_page_progress",
            `where manifest_run_id = ${sqlText2(input.manifestRunId)};`
          ].join(" ")
        )
      );
      const expectedPageNumber = (isNonNegativeInteger3(maxPageRow?.[0]) ? maxPageRow?.[0] : -1) + 1;
      if (input.pageNumber !== expectedPageNumber) {
        return block("device_manifest_page_invalid");
      }
      session.exec(
        [
          "insert into manifest_page_progress (manifest_run_id, page_number,",
          "entry_count, page_digest) values (",
          `${sqlText2(input.manifestRunId)}, ${input.pageNumber},`,
          `${input.entryCount}, ${sqlText2(input.pageDigest)});`
        ].join(" ")
      );
      if (state.activeManifestRunId === null) {
        session.exec(
          [
            "update device_sync_state set",
            `active_manifest_run_id = ${sqlText2(input.manifestRunId)},`,
            `manifest_checkpoint_sequence = ${input.checkpointSequence}`,
            "where singleton_key = 1;"
          ].join(" ")
        );
      }
    });
  }
  /**
   * Record one planned manifest action's local progress (spec 12.4): the
   * frozen action kind of an action index never changes, progress only
   * upgrades to `terminal_safe`, and a stale receipt never downgrades it.
   */
  async recordManifestAction(input) {
    validateManifestActionProgress(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (state.activeManifestRunId !== input.manifestRunId) {
        return block("device_manifest_state_invalid");
      }
      const existingAction = firstRow3(
        session.readRows(
          [
            "select action_kind, outcome from manifest_action_progress",
            `where manifest_run_id = ${sqlText2(input.manifestRunId)}`,
            `and action_index = ${input.actionIndex};`
          ].join(" ")
        )
      );
      if (existingAction === null) {
        session.exec(
          [
            "insert into manifest_action_progress (manifest_run_id, action_index,",
            "action_kind, outcome, safe_reason_code) values (",
            `${sqlText2(input.manifestRunId)}, ${input.actionIndex},`,
            `${sqlText2(input.actionKind)}, ${sqlText2(input.outcome)},`,
            `${sqlNullableText(input.reason)});`
          ].join(" ")
        );
        return;
      }
      const [actionKind, outcome] = existingAction;
      if (actionKind !== input.actionKind) {
        return block("device_manifest_state_invalid");
      }
      if (input.outcome === "terminal_safe" && outcome !== "terminal_safe") {
        session.exec(
          [
            "update manifest_action_progress set outcome = 'terminal_safe',",
            `safe_reason_code = ${sqlNullableText(input.reason)}`,
            `where manifest_run_id = ${sqlText2(input.manifestRunId)}`,
            `and action_index = ${input.actionIndex};`
          ].join(" ")
        );
      }
    });
  }
  /**
   * Persist the durable prepare of one remote apply operation (spec 8.1,
   * 10.3) BEFORE any Vault mutation. An exact re-prepare of the same
   * still-`prepared` operation is idempotent; a conflicting re-prepare
   * contradicts the durable evidence and blocks.
   */
  async prepareRemoteApply(input) {
    validatePreparedRemoteApply(input);
    await this.#runBlockedMutation((session, block) => {
      const existing = this.#readRemoteApplyRow(session, input.eventSequence);
      if (existing !== null) {
        if (existing.state !== "prepared" || !isSamePreparedOperation(existing, input)) {
          return block("device_apply_recovery_ambiguous");
        }
        return;
      }
      const baseFingerprint = input.baseFingerprint;
      const finalFingerprint = input.finalFingerprint;
      session.exec(
        [
          "insert into remote_apply_operations (event_sequence, event_id, source_id,",
          "operation, prior_locator, target_locator, base_sha256, base_size_bytes,",
          "base_media_type, final_sha256, final_size_bytes, final_media_type,",
          "temp_token, rollback_token, state) values (",
          `${input.eventSequence}, ${sqlText2(input.eventId)}, ${sqlText2(input.sourceId)},`,
          `${sqlText2(input.operation)}, ${sqlNullableText(input.priorLocator)},`,
          `${sqlNullableText(input.targetLocator)},`,
          `${sqlNullableText(baseFingerprint?.sha256 ?? null)},`,
          `${baseFingerprint === null ? "null" : baseFingerprint.sizeBytes},`,
          `${sqlNullableText(baseFingerprint?.mediaType ?? null)},`,
          `${sqlNullableText(finalFingerprint?.sha256 ?? null)},`,
          `${finalFingerprint === null ? "null" : finalFingerprint.sizeBytes},`,
          `${sqlNullableText(finalFingerprint?.mediaType ?? null)},`,
          `${sqlNullableText(input.tempToken)}, ${sqlNullableText(input.rollbackToken)},`,
          "'prepared');"
        ].join(" ")
      );
    });
  }
  /**
   * Abandon one `prepared` remote apply intent together with its echo
   * marker. The caller must have PROVEN the Vault sits at the operation's
   * verified pre-mutation expectation first (the crash-safe recovery's
   * clean verdict): nothing was mutated, so the durable intent and its
   * suppression marker are safe to drop — which unblocks both a fresh
   * prepare and the reconciler's synthetic apply at the same sequence (the
   * server never redelivers an already-delivered event, so the abandoned
   * intent is re-converged through manifest reconciliation instead).
   */
  async abandonRemoteApply(eventSequence) {
    if (!isNonNegativeInteger3(eventSequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const operation = this.#readRemoteApplyRow(session, eventSequence);
      if (operation === null || operation.state !== "prepared") {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        `delete from remote_apply_operations where event_sequence = ${eventSequence};`
      );
      session.exec(`delete from echo_markers where event_sequence = ${eventSequence};`);
    });
  }
  /**
   * Transition one remote apply operation along the legal lattice of
   * spec 8.1/11, persisting the opaque staging/rollback tokens that
   * become durable with the state. Illegal, backwards or unknown-
   * sequence transitions contradict the durable evidence and block.
   */
  async transitionRemoteApply(input) {
    validateRemoteApplyTransition(input);
    await this.#runBlockedMutation((session, block) => {
      const operation = this.#readRemoteApplyRow(session, input.eventSequence);
      if (operation === null) {
        return block("device_apply_recovery_ambiguous");
      }
      if (!isLegalRemoteApplyTransition(operation.operation, operation.state, input.state)) {
        return block("device_apply_recovery_ambiguous");
      }
      const tokenAssignments = [];
      if (input.tempToken !== void 0) {
        tokenAssignments.push(`temp_token = ${sqlNullableText(input.tempToken)}`);
      }
      if (input.rollbackToken !== void 0) {
        tokenAssignments.push(`rollback_token = ${sqlNullableText(input.rollbackToken)}`);
      }
      session.exec(
        [
          "update remote_apply_operations set",
          `state = ${sqlText2(input.state)}`,
          ...tokenAssignments.length > 0 ? [`, ${tokenAssignments.join(", ")}`] : [],
          `where event_sequence = ${input.eventSequence};`
        ].join(" ")
      );
    });
  }
  /**
   * Record one terminal-safe device event outcome AND advance the local
   * applied cursor in the SAME serialized generation (spec 11): an
   * `applied` outcome requires the durable `vault_mutated` proof (or an
   * already-`locally_applied` recovery row), and any other terminal
   * outcome closes a dangling prepared row with its closed reason. A
   * non-contiguous sequence is a gap or regression blocker — the cursor
   * never advances on it.
   */
  async terminalizeEvent(input) {
    validateTerminalDeviceEvent(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (input.eventSequence > state.appliedSequence + 1) {
        return block("device_cursor_gap");
      }
      if (input.eventSequence <= state.appliedSequence) {
        return block("device_cursor_regression");
      }
      const operation = this.#readRemoteApplyRow(session, input.eventSequence);
      if (input.outcome === "applied") {
        if (operation === null || operation.state === "prepared" || operation.state === "temp_verified") {
          return block("device_apply_recovery_ambiguous");
        }
        if (operation.state === "vault_mutated") {
          session.exec(
            `update remote_apply_operations set state = 'locally_applied' where event_sequence = ${input.eventSequence};`
          );
        }
      } else if (operation !== null && operation.state !== "locally_applied" && operation.state !== "server_acknowledged") {
        session.exec(
          [
            "update remote_apply_operations set state = 'locally_applied',",
            `safe_error_code = ${sqlNullableText(input.reason)}`,
            `where event_sequence = ${input.eventSequence};`
          ].join(" ")
        );
      }
      session.exec(
        `update device_sync_state set applied_sequence = ${input.eventSequence} where singleton_key = 1;`
      );
    });
  }
  /**
   * Record the server's cursor acknowledgement (spec 7.2, 11). The
   * acknowledgement never runs ahead of the applied cursor (the debt
   * invariant), never regresses, and an exact replay is an idempotent
   * no-op. Every locally-applied operation through the acknowledged
   * sequence is marked server-acknowledged.
   */
  async recordServerAcknowledgement(sequence) {
    if (!isNonNegativeInteger3(sequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (sequence < state.acknowledgedSequence) {
        return block("device_cursor_regression");
      }
      if (sequence === state.acknowledgedSequence) {
        return;
      }
      if (sequence > state.appliedSequence) {
        return block("device_cursor_ack_ahead");
      }
      session.exec(
        `update device_sync_state set acknowledged_sequence = ${sequence} where singleton_key = 1;`
      );
      session.exec(
        [
          "update remote_apply_operations set state = 'server_acknowledged'",
          `where event_sequence <= ${sequence} and state = 'locally_applied';`
        ].join(" ")
      );
    });
  }
  /**
   * Complete one repair run (spec 7.3, 12.4): the exact planned run, its
   * checkpoint and the barrier generation that started the repair must
   * all match the durable state. One transaction advances both cursors
   * to the checkpoint (the server authorized its cursor to `C` in the
   * same completion), clears the barrier and run fields, discards the
   * run's temporary page/action progress and settles every locally
   * applied operation through the checkpoint.
   */
  async completeRepair(input) {
    validateCompleteLocalRepair(input);
    await this.#runBlockedMutation((session, block) => {
      const state = this.#readState(session);
      if (state.barrierGeneration === null || state.barrierGeneration !== input.barrierGeneration || state.activeManifestRunId !== input.manifestRunId) {
        return block("device_manifest_state_invalid");
      }
      if (input.checkpointSequence < state.appliedSequence) {
        return block("device_cursor_regression");
      }
      session.exec(
        [
          "update device_sync_state set",
          `applied_sequence = ${input.checkpointSequence},`,
          `acknowledged_sequence = ${input.checkpointSequence},`,
          "barrier_generation = null, barrier_reason = null,",
          "active_manifest_run_id = null, manifest_checkpoint_sequence = null,",
          "manifest_final_digest = null",
          "where singleton_key = 1;"
        ].join(" ")
      );
      session.exec(
        `delete from manifest_page_progress where manifest_run_id = ${sqlText2(input.manifestRunId)};`
      );
      session.exec(
        `delete from manifest_action_progress where manifest_run_id = ${sqlText2(input.manifestRunId)};`
      );
      session.exec(
        [
          "update remote_apply_operations set state = 'server_acknowledged'",
          `where event_sequence <= ${input.checkpointSequence} and state = 'locally_applied';`
        ].join(" ")
      );
    });
  }
  /**
   * The oldest remote apply operation that still owes work (spec 11):
   * anything not yet server-acknowledged, lowest event sequence first.
   */
  readUnfinishedApply() {
    const row = firstRow3(
      this.#database.readAll(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          "where state != 'server_acknowledged'",
          "order by event_sequence asc limit 1;"
        ].join(" ")
      )
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }
  /**
   * One remote apply operation by its exact event sequence, or null
   * (read-only). The settle path of a repeatedly refused vault write
   * addresses the failed event's OWN row this way — the oldest-unfinished
   * read can name an earlier still-unacknowledged row instead.
   */
  readRemoteApply(eventSequence) {
    if (!isPositiveInteger5(eventSequence)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow3(
      this.#database.readAll(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          `where event_sequence = ${eventSequence};`
        ].join(" ")
      )
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }
  /**
   * Record one exact echo marker (spec 8.2) before the Vault mutation.
   * An exact duplicate is a no-op; a conflicting duplicate for one event
   * sequence contradicts the immutable server event and is refused.
   */
  async recordEchoMarker(input) {
    validateEchoMarker(input);
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readEchoMarkerRow(session, input.eventSequence);
      if (existing !== null) {
        if (!isSameEchoMarker(existing, input)) {
          throw journalStoreError("journal_mutation_failed");
        }
        return;
      }
      const finalFingerprint = input.finalFingerprint;
      session.exec(
        [
          "insert into echo_markers (event_sequence, source_id, operation,",
          "prior_locator, target_locator, final_sha256, final_size_bytes,",
          "final_media_type) values (",
          `${input.eventSequence}, ${sqlText2(input.sourceId)}, ${sqlText2(input.operation)},`,
          `${sqlNullableText(input.priorLocator)}, ${sqlNullableText(input.targetLocator)},`,
          `${sqlNullableText(finalFingerprint?.sha256 ?? null)},`,
          `${finalFingerprint === null ? "null" : finalFingerprint.sizeBytes},`,
          `${sqlNullableText(finalFingerprint?.mediaType ?? null)});`
        ].join(" ")
      );
    });
  }
  /** One exact echo marker by its event sequence, or null (read-only). */
  readEchoMarker(eventSequence) {
    if (!isPositiveInteger5(eventSequence)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow3(
      this.#database.readAll(
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers where event_sequence = ${eventSequence};`
      )
    );
    return row === null ? null : parseEchoMarkerRow(row);
  }
  /**
   * Match one watcher/recovery observation against the exact echo marker
   * of its event sequence (spec 8.2): every applicable member — source,
   * operation, prior/target locator, expected final fingerprint — must
   * match. Only an exact match consumes the marker; a mismatch keeps it.
   */
  async matchAndConsumeEcho(input) {
    if (!isPositiveInteger5(input.eventSequence)) {
      throw journalStoreError("journal_mutation_failed");
    }
    return this.#database.runSerializedMutation((session) => {
      const marker = this.#readEchoMarkerRow(session, input.eventSequence);
      if (marker === null || !isExactEchoMatch(marker, input)) {
        return false;
      }
      session.exec(`delete from echo_markers where event_sequence = ${input.eventSequence};`);
      return true;
    });
  }
  // --- internals --------------------------------------------------------------------------------------
  #readState(session) {
    const reader = {
      readAll: (sql) => session.readRows(sql)
    };
    return readDeviceSyncState(reader);
  }
  #readRemoteApplyRow(session, eventSequence) {
    const row = firstRow3(
      session.readRows(
        [
          `select ${REMOTE_APPLY_OPERATION_COLUMNS.join(", ")} from remote_apply_operations`,
          `where event_sequence = ${eventSequence};`
        ].join(" ")
      )
    );
    return row === null ? null : parseRemoteApplyRow(row);
  }
  #readEchoMarkerRow(session, eventSequence) {
    const row = firstRow3(
      session.readRows(
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers where event_sequence = ${eventSequence};`
      )
    );
    return row === null ? null : parseEchoMarkerRow(row);
  }
  /**
   * Run one serialized mutation whose INVARIANT blockers persist the
   * closed barrier reason inside the same transaction and then reject
   * with the closed `journal_mutation_failed` store reason: the blocker
   * stays readable through status while the failure still propagates.
   * Ordinary store errors roll the whole transaction back untouched.
   */
  async #runBlockedMutation(operation) {
    let blockedReason = null;
    await this.#database.runSerializedMutation(
      (session) => operation(session, (reason) => {
        const state = this.#readState(session);
        const barrierGeneration = state.barrierGeneration ?? state.observationGeneration;
        session.exec(
          [
            "update device_sync_state set",
            `barrier_generation = ${barrierGeneration},`,
            `barrier_reason = ${sqlText2(reason)}`,
            "where singleton_key = 1;"
          ].join(" ")
        );
        blockedReason = reason;
      })
    );
    if (blockedReason !== null) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
};
function isSamePreparedOperation(stored, input) {
  return stored.eventId === input.eventId && stored.sourceId === input.sourceId && stored.operation === input.operation && stored.priorLocator === input.priorLocator && stored.targetLocator === input.targetLocator && isSameFingerprint(stored.baseFingerprint, input.baseFingerprint) && isSameFingerprint(stored.finalFingerprint, input.finalFingerprint) && stored.tempToken === input.tempToken && stored.rollbackToken === input.rollbackToken;
}
function isSameEchoMarker(stored, input) {
  return stored.sourceId === input.sourceId && stored.operation === input.operation && stored.priorLocator === input.priorLocator && stored.targetLocator === input.targetLocator && isSameFingerprint(stored.finalFingerprint, input.finalFingerprint);
}
function isExactEchoMatch(marker, observation) {
  if (observation.sourceId === null || observation.sourceId !== marker.sourceId) {
    return false;
  }
  if (observation.operation === null || observation.operation !== marker.operation) {
    return false;
  }
  if (marker.priorLocator !== null && observation.priorLocator !== marker.priorLocator) {
    return false;
  }
  if (marker.targetLocator !== null && observation.targetLocator !== marker.targetLocator) {
    return false;
  }
  if (marker.finalFingerprint !== null && !isSameFingerprint(observation.fingerprint, marker.finalFingerprint)) {
    return false;
  }
  return true;
}

// src/journal/note-status.ts
var LIFECYCLE_OPERATIONS = /* @__PURE__ */ new Set([
  "rename",
  "move",
  "delete",
  "restore"
]);
function projectLocalNoteSyncStatus(input) {
  const base = {
    normalizedPath: input.normalizedPath,
    policyRevisionNumber: input.policyRevisionNumber
  };
  if (input.isReconcileRequired || input.latestEvent === null) {
    return { ...base, state: "reconcile_required", retryAtEpochMs: null, reason: null };
  }
  const event = input.latestEvent;
  switch (event.state) {
    case "queued":
      return { ...base, state: "queued", retryAtEpochMs: null, reason: null };
    case "preflight":
    case "uploading":
      return { ...base, state: "syncing", retryAtEpochMs: null, reason: null };
    case "waiting_retry":
      return {
        ...base,
        state: "retrying",
        retryAtEpochMs: event.nextEligibleRetryEpochMs,
        reason: event.safeError
      };
    case "excluded_policy":
      return { ...base, state: "policy_blocked", retryAtEpochMs: null, reason: event.safeError };
    case "blocked_conflict":
      return { ...base, state: "conflict", retryAtEpochMs: null, reason: event.safeError };
    case "committed":
    case "no_change": {
      const verdictFingerprint = LIFECYCLE_OPERATIONS.has(event.operation) ? input.lastCommittedFingerprint : event.fingerprint;
      return fingerprintsMatch2(verdictFingerprint, input.observedFingerprint) ? { ...base, state: "synced", retryAtEpochMs: null, reason: null } : { ...base, state: "reconcile_required", retryAtEpochMs: null, reason: null };
    }
    case "blocked_size":
    case "deferred_lifecycle":
    case "integrity_failed":
      return {
        ...base,
        state: "reconcile_required",
        retryAtEpochMs: null,
        reason: event.safeError
      };
  }
}
function fingerprintsMatch2(left, right) {
  return left !== null && left.sha256 === right.sha256 && left.sizeBytes === right.sizeBytes && left.mediaType === right.mediaType;
}

// src/journal/repository.ts
var PENDING_RENAME_MISSING_FILE_MAX_DEFERRALS = 40;
var UUID_PATTERN5 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var MAX_REQUEST_CORRELATION_ID_LENGTH = 128;
var OPERATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
function isOperationTokenShape(value) {
  return typeof value === "string" && OPERATION_TOKEN_PATTERN.test(value);
}
function sqlText3(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
function isUuid4(value) {
  return UUID_PATTERN5.test(value);
}
function isNonNegativeInteger4(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function isClosedToken2(value, closedSet) {
  return typeof value === "string" && closedSet.includes(value);
}
function validateNormalizedPath2(normalizedPath) {
  if (typeof normalizedPath !== "string" || normalizedPath.length === 0 || normalizedPath.normalize("NFC") !== normalizedPath || normalizedPath.includes("\\") || normalizedPath.startsWith("/") || normalizedPath.endsWith("/")) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const character of normalizedPath) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 32 || codeUnit === 127) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  const segments = normalizedPath.split("/");
  if (segments[0]?.includes(":")) {
    throw journalStoreError("journal_mutation_failed");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw journalStoreError("journal_mutation_failed");
    }
  }
}
function validateCaptureInput(input) {
  validateNormalizedPath2(input.normalizedPath);
  if (!isFrozenFingerprintShape(input.fingerprint)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger4(input.policyRevisionNumber)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isClosedToken2(input.admission, JOURNAL_CAPTURE_ADMISSIONS)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
var MULTIPART_SESSION_ID_MIN_LENGTH = 32;
var MULTIPART_SESSION_ID_MAX_LENGTH = 128;
var MULTIPART_SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
function isMultipartSessionIdShape(value) {
  return typeof value === "string" && value.length >= MULTIPART_SESSION_ID_MIN_LENGTH && value.length <= MULTIPART_SESSION_ID_MAX_LENGTH && MULTIPART_SESSION_ID_PATTERN.test(value) && !isUuid4(value);
}
var MULTIPART_PROGRESS_RECORD_KEYS = [
  "completedPartNumbers",
  "eventId",
  "expiresAtEpochMs",
  "partCount",
  "partSizeBytes",
  "safeReason",
  "sessionId",
  "sessionState"
];
function validateMultipartProgressRecord(record) {
  if (typeof record !== "object" || record === null) {
    throw journalStoreError("journal_mutation_failed");
  }
  const keys = Object.keys(record).sort();
  const expectedKeys = [...MULTIPART_PROGRESS_RECORD_KEYS].sort();
  if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index])) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isUuid4(record.eventId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isMultipartSessionIdShape(record.sessionId)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof record.partSizeBytes !== "number" || !Number.isInteger(record.partSizeBytes) || record.partSizeBytes !== MULTIPART_PART_SIZE_BYTES) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (typeof record.partCount !== "number" || !Number.isInteger(record.partCount) || record.partCount < 1 || record.partCount > MAX_MULTIPART_PART_COUNT) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!isNonNegativeInteger4(record.expiresAtEpochMs)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (!Array.isArray(record.completedPartNumbers)) {
    throw journalStoreError("journal_mutation_failed");
  }
  let previousPartNumber = 0;
  for (const partNumber of record.completedPartNumbers) {
    if (typeof partNumber !== "number" || !Number.isInteger(partNumber) || partNumber < 1 || partNumber > record.partCount || partNumber <= previousPartNumber) {
      throw journalStoreError("journal_mutation_failed");
    }
    previousPartNumber = partNumber;
  }
  if (!isClosedToken2(record.sessionState, MULTIPART_SESSION_STATES)) {
    throw journalStoreError("journal_mutation_failed");
  }
  if (record.safeReason !== null && !isClosedToken2(record.safeReason, MULTIPART_SAFE_REASON_TOKENS)) {
    throw journalStoreError("journal_mutation_failed");
  }
}
var LOCAL_FILE_COLUMNS = [
  "local_file_id",
  "normalized_path",
  "source_id",
  "observed_sha256",
  "observed_size_bytes",
  "observed_media_type",
  "base_version_id",
  "policy_revision",
  "last_committed_sha256",
  "last_committed_size_bytes",
  "last_committed_media_type"
];
var JOURNAL_EVENT_COLUMNS = [
  "event_id",
  "local_file_id",
  "idempotency_key",
  "operation",
  "sha256",
  "size_bytes",
  "media_type",
  "state",
  "is_fingerprint_frozen",
  "attempt_count",
  "next_eligible_retry_epoch_ms",
  "safe_error",
  "operation_id",
  "created_at_epoch_ms"
];
var JOURNAL_ATTEMPT_COLUMNS = [
  "event_id",
  "attempted_at_epoch_ms",
  "outcome_label",
  "request_correlation_id"
];
function isNullableText3(value) {
  return value === null || typeof value === "string";
}
function isNullableNonNegativeInteger3(value) {
  return value === null || typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function parseLocalFileRow(row) {
  const [
    localFileId,
    normalizedPath,
    sourceId,
    observedSha256,
    observedSizeBytes,
    observedMediaType,
    baseVersionId,
    policyRevision,
    lastCommittedSha256,
    lastCommittedSizeBytes,
    lastCommittedMediaType
  ] = row;
  if (typeof localFileId !== "string" || typeof normalizedPath !== "string" || !isNullableText3(sourceId) || typeof observedSha256 !== "string" || typeof observedSizeBytes !== "number" || typeof observedMediaType !== "string" || !isNullableText3(baseVersionId) || typeof policyRevision !== "number") {
    throw journalStoreError("journal_image_invalid");
  }
  const lastCommittedFingerprint = typeof lastCommittedSha256 === "string" && typeof lastCommittedSizeBytes === "number" && typeof lastCommittedMediaType === "string" ? {
    sha256: lastCommittedSha256,
    sizeBytes: lastCommittedSizeBytes,
    mediaType: lastCommittedMediaType
  } : null;
  return {
    localFileId,
    normalizedPath,
    sourceId,
    observedFingerprint: {
      sha256: observedSha256,
      sizeBytes: observedSizeBytes,
      mediaType: observedMediaType
    },
    baseVersionId,
    policyRevisionNumber: policyRevision,
    lastCommittedFingerprint
  };
}
function parseJournalEventRow(row) {
  const [
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    sha256,
    sizeBytes,
    mediaType,
    state,
    isFingerprintFrozen,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId
  ] = row;
  if (typeof eventId !== "string" || typeof localFileId !== "string" || typeof idempotencyKey !== "string" || !isClosedToken2(String(operation), [...JOURNAL_OPERATIONS]) || typeof sha256 !== "string" || typeof sizeBytes !== "number" || typeof mediaType !== "string" || typeof state !== "string" || !isClosedToken2(state, JOURNAL_EVENT_STATES) || isFingerprintFrozen !== 0 && isFingerprintFrozen !== 1 || typeof attemptCount !== "number" || !isNullableNonNegativeInteger3(nextEligibleRetryEpochMs) || !isNullableText3(safeError) || !isNullableText3(operationId)) {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    fingerprint: { sha256, sizeBytes, mediaType },
    state,
    isFingerprintFrozen: isFingerprintFrozen === 1,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId
  };
}
function parseJournalAttemptRow(row) {
  const [eventId, attemptedAtEpochMs, outcomeLabel, requestCorrelationId] = row;
  if (typeof eventId !== "string" || typeof attemptedAtEpochMs !== "number" || typeof outcomeLabel !== "string" || !isClosedToken2(outcomeLabel, JOURNAL_SAFE_ERROR_LABELS) || typeof requestCorrelationId !== "string") {
    throw journalStoreError("journal_image_invalid");
  }
  return {
    eventId,
    attemptedAtEpochMs,
    outcomeLabel,
    requestCorrelationId
  };
}
function parseMultipartProgressRow(row, eventId) {
  const [
    sessionId,
    partSizeBytes,
    partCount,
    expiresAtEpochMs,
    completedPartNumbersJson,
    sessionState,
    safeReason
  ] = row;
  let completedPartNumbers;
  if (typeof completedPartNumbersJson !== "string") {
    throw journalStoreError("journal_image_invalid");
  }
  try {
    completedPartNumbers = JSON.parse(completedPartNumbersJson);
  } catch {
    throw journalStoreError("journal_image_invalid");
  }
  const candidate = {
    eventId,
    sessionId,
    partSizeBytes,
    partCount,
    expiresAtEpochMs,
    completedPartNumbers,
    sessionState,
    safeReason
  };
  try {
    validateMultipartProgressRecord(candidate);
  } catch (error) {
    if (error instanceof JournalStoreError) {
      throw journalStoreError("journal_image_invalid");
    }
    throw error;
  }
  return candidate;
}
function parseLocalNoteSyncStatusRow(row, isReconcileRequired) {
  const [
    normalizedPath,
    policyRevisionNumber,
    observedSha256,
    observedSizeBytes,
    observedMediaType,
    lastCommittedSha256,
    lastCommittedSizeBytes,
    lastCommittedMediaType,
    ...eventRow
  ] = row;
  if (typeof normalizedPath !== "string" || typeof policyRevisionNumber !== "number" || !Number.isInteger(policyRevisionNumber) || policyRevisionNumber < 0 || typeof observedSha256 !== "string" || typeof observedSizeBytes !== "number" || !Number.isInteger(observedSizeBytes) || observedSizeBytes < 0 || typeof observedMediaType !== "string") {
    throw journalStoreError("journal_image_invalid");
  }
  const lastCommittedFingerprint = typeof lastCommittedSha256 === "string" && typeof lastCommittedSizeBytes === "number" && typeof lastCommittedMediaType === "string" ? {
    sha256: lastCommittedSha256,
    sizeBytes: lastCommittedSizeBytes,
    mediaType: lastCommittedMediaType
  } : null;
  const latestEvent = eventRow[0] === null ? null : parseJournalEventRow(eventRow);
  if (latestEvent === null && eventRow.some((value) => value !== null)) {
    throw journalStoreError("journal_image_invalid");
  }
  return projectLocalNoteSyncStatus({
    normalizedPath,
    policyRevisionNumber,
    observedFingerprint: {
      sha256: observedSha256,
      sizeBytes: observedSizeBytes,
      mediaType: observedMediaType
    },
    lastCommittedFingerprint,
    latestEvent,
    isReconcileRequired
  });
}
function toPublicEvent(event) {
  const { eventId, localFileId, idempotencyKey, operation, fingerprint, state, attemptCount, nextEligibleRetryEpochMs, safeError, operationId } = event;
  return {
    eventId,
    localFileId,
    idempotencyKey,
    operation,
    fingerprint,
    state,
    attemptCount,
    nextEligibleRetryEpochMs,
    safeError,
    operationId
  };
}
function firstRow4(result) {
  return result[0]?.values[0] ?? null;
}
function selectColumns(table, columns, suffix) {
  return `select ${columns.join(", ")} from ${table} ${suffix};`;
}
var JournalRepository = class {
  #database;
  #createId;
  #nowEpochMs;
  #lifecycle;
  #deviceSync;
  #onDeviceSyncRepairComplete;
  constructor(options) {
    this.#database = options.database;
    this.#createId = options.createId ?? (() => crypto.randomUUID());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    if (options.createLifecycleRepository) {
      this.#lifecycle = options.createLifecycleRepository({
        database: this.#database,
        createId: this.#createId,
        nowEpochMs: this.#nowEpochMs
      });
    } else {
      this.#lifecycle = new LifecycleRepository({
        database: this.#database,
        createId: this.#createId,
        nowEpochMs: this.#nowEpochMs
      });
    }
    this.#deviceSync = options.createDeviceSyncRepository ? options.createDeviceSyncRepository({ database: this.#database }) : new DeviceSyncRepository({ database: this.#database });
    this.#onDeviceSyncRepairComplete = options.onDeviceSyncRepairComplete ?? null;
  }
  /** The lifecycle repository wired against the same writer. */
  get lifecycle() {
    return this.#lifecycle;
  }
  /** The current byte-read endpoint of this content owner's durable rename chain. */
  readPendingRenameIntentForLocalFile(localFileId) {
    return this.#lifecycle.readPendingRenameIntentForLocalFile(localFileId);
  }
  /**
   * The device-sync reconciliation repository (task 8) wired against the
   * same writer: cursor, barrier, manifest progress, remote apply and
   * echo state persist through the single serialized queue.
   */
  get deviceSync() {
    return this.#deviceSync;
  }
  // --- device-sync manifest reconciliation (task 11, spec 12.4, 8.2) ----------------------
  /**
   * Complete one device repair run and clear the journal's
   * `reconcile_required` flag (spec 12.4): the composition's
   * reconcile-complete notification fires FIRST (so a persistence-composed
   * journal honors the clear through its sticky merge), the device-sync
   * completion advances both cursors to the checkpoint and clears the
   * barrier/run fields, and one following transaction clears the flag and
   * retires every echo marker at or below the newly acknowledged cursor
   * (spec 8.2 — no time-based expiry exists).
   */
  async completeDeviceSyncRepair(input) {
    this.#onDeviceSyncRepairComplete?.();
    await this.#deviceSync.completeRepair(input);
    await this.#database.runSerializedMutation((session) => {
      const meta = session.readJournalMeta();
      if (meta.isReconcileRequired) {
        session.writeJournalMeta({ ...meta, isReconcileRequired: false });
      }
      session.exec(
        [
          "delete from echo_markers where event_sequence <=",
          "(select acknowledged_sequence from device_sync_state where singleton_key = 1);"
        ].join(" ")
      );
    });
  }
  /**
   * Discard ONLY the temporary run progress of the active manifest run
   * (spec 7.3, 9.1 — a one-hour expiry or a policy advance): the active
   * run fields and every page/action progress row clear while the repair
   * barrier, the cursors and every local edit stay untouched, so the next
   * run starts checkpoint-bound from the current barrier.
   */
  async discardActiveManifestRun() {
    await this.#database.runSerializedMutation((session) => {
      const state = readDeviceSyncState({ readAll: (sql) => session.readRows(sql) });
      if (state.barrierGeneration === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update device_sync_state set active_manifest_run_id = null,",
          "manifest_checkpoint_sequence = null, manifest_final_digest = null",
          "where singleton_key = 1;"
        ].join(" ")
      );
      session.exec("delete from manifest_page_progress;");
      session.exec("delete from manifest_action_progress;");
    });
  }
  /** One recorded page of the active manifest run: ordered number, entry count, digest. */
  readManifestPageProgress() {
    const state = this.#deviceSync.readState();
    if (state.activeManifestRunId === null) {
      return [];
    }
    const result = this.#database.readAll(
      [
        "select page_number, entry_count, page_digest from manifest_page_progress",
        `where manifest_run_id = ${sqlText3(state.activeManifestRunId)}`,
        "order by page_number asc;"
      ].join(" ")
    );
    return (result[0]?.values ?? []).map((row) => {
      const [pageNumber, entryCount, pageDigest] = row;
      if (typeof pageNumber !== "number" || !Number.isInteger(pageNumber) || pageNumber < 0 || typeof entryCount !== "number" || !Number.isInteger(entryCount) || entryCount < 0 || typeof pageDigest !== "string") {
        throw journalStoreError("journal_image_invalid");
      }
      return { pageNumber, entryCount, pageDigest };
    });
  }
  /** One recorded action progress row of the active manifest run (ordered by action index). */
  readManifestActionProgress() {
    const state = this.#deviceSync.readState();
    if (state.activeManifestRunId === null) {
      return [];
    }
    const result = this.#database.readAll(
      [
        "select action_index, action_kind, outcome, safe_reason_code from manifest_action_progress",
        `where manifest_run_id = ${sqlText3(state.activeManifestRunId)}`,
        "order by action_index asc;"
      ].join(" ")
    );
    return (result[0]?.values ?? []).map((row) => {
      const [actionIndex, actionKind, outcome, reason] = row;
      if (typeof actionIndex !== "number" || !Number.isInteger(actionIndex) || actionIndex < 0 || typeof actionKind !== "string" || !MANIFEST_ACTION_KINDS.includes(actionKind) || typeof outcome !== "string" || !MANIFEST_ACTION_PROGRESS_OUTCOMES.includes(outcome) || reason !== null && !isDeviceSyncReason(reason)) {
        throw journalStoreError("journal_image_invalid");
      }
      return {
        actionIndex,
        actionKind,
        outcome,
        reason
      };
    });
  }
  // --- capture (spec 6.3, 7.1, 7.2) ---------------------------------------------------------
  /**
   * Record one settled capture. A `policy_allowed` capture replaces the
   * fingerprint of the file's unfrozen `queued`/`waiting_retry` event
   * (coalescing, spec 7.2) or appends a new event — born terminal for the
   * two fail-closed blocked admissions. New rows are refused once either
   * queue soft limit is reached; the refusal flags `reconcile_required`
   * durably and preserves every existing row (spec 6.4).
   */
  async recordCapture(input) {
    validateCaptureInput(input);
    return this.#database.runSerializedMutation((session) => {
      const read = (sql) => session.readRows(sql);
      const exec = (sql) => session.exec(sql);
      const existingFile = this.#readLocalFileRow(read, input.normalizedPath);
      const localFileId = existingFile?.localFileId ?? this.#createId();
      const operation = existingFile?.sourceId != null ? "update" : "create";
      const coalescableEvent = input.admission === "policy_allowed" ? this.#readCoalescableEventRow(session, localFileId) : null;
      if (coalescableEvent !== null) {
        session.exec(
          [
            "update journal_events set",
            `operation = ${sqlText3(operation)},`,
            `sha256 = ${sqlText3(input.fingerprint.sha256)},`,
            `size_bytes = ${input.fingerprint.sizeBytes},`,
            `media_type = ${sqlText3(input.fingerprint.mediaType)}`,
            `where event_id = ${sqlText3(coalescableEvent.eventId)};`
          ].join(" ")
        );
        this.#writeObservedFingerprint(exec, localFileId, input);
        return {
          outcome: "event_coalesced",
          event: toPublicEvent({ ...coalescableEvent, operation, fingerprint: input.fingerprint }),
          localFile: this.#requireLocalFileRow(read, input.normalizedPath)
        };
      }
      if (this.#isAtQueueLimit(session)) {
        this.#persistReconcileRequired(session);
        return { outcome: "capture_refused", reason: "reconcile_required" };
      }
      const initialState = input.admission === "policy_allowed" ? "queued" : input.admission;
      const eventId = this.#createId();
      const idempotencyKey = this.#createId();
      if (existingFile === null) {
        session.exec(
          [
            "insert into local_files (local_file_id, normalized_path, source_id,",
            "observed_sha256, observed_size_bytes, observed_media_type, base_version_id,",
            `policy_revision) values (${sqlText3(localFileId)},`,
            `${sqlText3(input.normalizedPath)}, null,`,
            `${sqlText3(input.fingerprint.sha256)}, ${input.fingerprint.sizeBytes},`,
            `${sqlText3(input.fingerprint.mediaType)}, null, ${input.policyRevisionNumber});`
          ].join(" ")
        );
      } else {
        this.#writeObservedFingerprint(exec, localFileId, input);
      }
      session.exec(
        [
          "insert into journal_events (event_id, local_file_id, idempotency_key, operation,",
          "sha256, size_bytes, media_type, state, is_fingerprint_frozen, attempt_count,",
          "safe_error, created_at_epoch_ms) values (",
          `${sqlText3(eventId)}, ${sqlText3(localFileId)}, ${sqlText3(idempotencyKey)},`,
          `${sqlText3(operation)}, ${sqlText3(input.fingerprint.sha256)},`,
          `${input.fingerprint.sizeBytes}, ${sqlText3(input.fingerprint.mediaType)},`,
          `${sqlText3(initialState)},`,
          `${input.admission === "policy_allowed" ? 0 : 1}, 0,`,
          `${input.admission === "policy_allowed" ? "null" : sqlText3(input.admission)},`,
          `${this.#nowEpochMs()});`
        ].join(" ")
      );
      if (this.#isAtQueueLimit(session)) {
        this.#persistReconcileRequired(session);
      }
      const event = {
        eventId,
        localFileId,
        idempotencyKey,
        operation,
        fingerprint: input.fingerprint,
        state: initialState,
        attemptCount: 0,
        nextEligibleRetryEpochMs: null,
        safeError: input.admission === "policy_allowed" ? null : input.admission,
        operationId: null
      };
      return {
        outcome: "event_recorded",
        event,
        localFile: this.#requireLocalFileRow(read, input.normalizedPath)
      };
    });
  }
  // --- event transitions (spec 7.2) ------------------------------------------------------------
  /**
   * Move one eligible event into `preflight`, freezing its fingerprint from
   * this moment on: the event and its fingerprint never change afterwards,
   * and any later save becomes a successor event (spec 7.2).
   */
  async markEventPreflightStarted(eventId) {
    if (!isUuid4(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId
      );
      if (!isClosedToken2(event.state, JOURNAL_COALESCABLE_EVENT_STATES)) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        `update journal_events set state = 'preflight', is_fingerprint_frozen = 1 where event_id = ${sqlText3(eventId)};`
      );
    });
  }
  /**
   * Record one retryable failure: the event returns to `waiting_retry` with
   * a closed safe error label and its next eligible retry time. Terminal
   * states never receive this transition (spec 7.2, 12).
   */
  async markEventWaitingRetry(eventId, safeError, nextEligibleRetryEpochMs) {
    if (!isUuid4(eventId) || !isClosedToken2(safeError, JOURNAL_SAFE_ERROR_LABELS) || !isNonNegativeInteger4(nextEligibleRetryEpochMs)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set state = 'waiting_retry',",
          "attempt_count = attempt_count + 1,",
          `next_eligible_retry_epoch_ms = ${nextEligibleRetryEpochMs},`,
          `safe_error = ${sqlText3(safeError)}`,
          `where event_id = ${sqlText3(eventId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Move one prefrozen event into `uploading`, persisting the opaque server
   * upload operation ID before the content stream may start (spec 7.2, 10.1:
   * every state transition lands before the next network action). The token
   * grammar mirrors the server's opaque operation handle: printable
   * URL-safe base64url of 32 to 128 characters.
   */
  async markEventUploading(eventId, operationId) {
    if (!isUuid4(eventId) || !isOperationTokenShape(operationId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId
      );
      if (event.state !== "preflight" && event.state !== "uploading") {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update journal_events set state = 'uploading',",
          `operation_id = ${sqlText3(operationId)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText3(eventId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Close one event in a terminal non-retry state with a closed safe error
   * label. Terminal rows stay queryable forever; they are never deleted and
   * never transition again (spec 6.4, 7.2). Any multipart progress of the
   * event clears in this same mutation: the terminal outcome (with its
   * closed label) is the durable evidence, and progress of a session that
   * can never dispatch again must not linger or resurrect.
   */
  async markEventTerminal(eventId, terminalState, safeError) {
    if (!isUuid4(eventId) || !isClosedToken2(terminalState, JOURNAL_NON_RETRY_EVENT_STATES) || !isClosedToken2(safeError, JOURNAL_SAFE_ERROR_LABELS)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        eventId
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set",
          `state = ${sqlText3(terminalState)},`,
          "next_eligible_retry_epoch_ms = null,",
          `safe_error = ${sqlText3(safeError)},`,
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText3(eventId)};`
        ].join(" ")
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText3(eventId)};`
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where event_id = ${sqlText3(eventId)};`
      );
    });
  }
  /**
   * Atomically park or close a content event whose current rename endpoint
   * disappeared. A durable intent keeps the identity-establishing event
   * retryable through forty accepted parks; the next matching call transfers
   * locator ownership to reconciliation. A counter bound to another event is
   * an invariant failure and takes that same reconciliation exit before any
   * increment or cutoff evaluation.
   */
  async resolveIntentAwareLocalFileMissing(input) {
    this.#validateIntentAwareAttemptInput(input, "deferred_lifecycle");
    return this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow((sql) => session.readRows(sql), input.eventId);
      this.#requireNonTerminalEvent(event);
      this.#requireContentEvent(event);
      this.#recordAttemptInSession(session, {
        eventId: input.eventId,
        attemptedAtEpochMs: input.attemptedAtEpochMs,
        outcomeLabel: "deferred_lifecycle",
        requestCorrelationId: input.requestCorrelationId
      });
      const intent = this.#readPendingRenameIntentInSession(session, event.localFileId);
      if (intent === null) {
        this.#closeContentEventInSession(
          session,
          event,
          "deferred_lifecycle",
          "deferred_lifecycle"
        );
        return { outcome: "closed_deferred_lifecycle" };
      }
      const deferral = firstRow4(
        session.readRows(
          [
            "select event_id, deferred_attempt_count",
            "from pending_rename_intent_missing_file_deferrals",
            `where local_file_id = ${sqlText3(event.localFileId)};`
          ].join(" ")
        )
      );
      if (deferral !== null) {
        const [storedEventId, storedCount] = deferral;
        if (typeof storedEventId !== "string" || !isUuid4(storedEventId) || typeof storedCount !== "number" || !Number.isInteger(storedCount) || storedCount < 1 || storedCount > PENDING_RENAME_MISSING_FILE_MAX_DEFERRALS) {
          throw journalStoreError("journal_image_invalid");
        }
        if (storedEventId !== input.eventId) {
          this.#closeContentEventInSession(
            session,
            event,
            "deferred_lifecycle",
            "deferred_lifecycle"
          );
          this.#reparentAndClearPendingRenameIntentInSession(session, event.localFileId, intent.currentPath, true);
          return {
            outcome: "reconcile_takeover",
            diagnosticReason: "pending_rename_intent_conflict"
          };
        }
        if (storedCount === PENDING_RENAME_MISSING_FILE_MAX_DEFERRALS) {
          this.#closeContentEventInSession(
            session,
            event,
            "deferred_lifecycle",
            "deferred_lifecycle"
          );
          this.#reparentAndClearPendingRenameIntentInSession(session, event.localFileId, intent.currentPath, true);
          return {
            outcome: "reconcile_takeover",
            diagnosticReason: "pending_rename_intent_exhausted"
          };
        }
        session.exec(
          [
            "update pending_rename_intent_missing_file_deferrals set",
            `deferred_attempt_count = ${storedCount + 1}`,
            `where local_file_id = ${sqlText3(event.localFileId)};`
          ].join(" ")
        );
      } else {
        session.exec(
          [
            "insert into pending_rename_intent_missing_file_deferrals",
            "(local_file_id, event_id, deferred_attempt_count) values (",
            `${sqlText3(event.localFileId)}, ${sqlText3(event.eventId)}, 1);`
          ].join(" ")
        );
      }
      session.exec(
        [
          "update journal_events set state = 'waiting_retry',",
          "attempt_count = attempt_count + 1,",
          `next_eligible_retry_epoch_ms = ${input.nextEligibleRetryEpochMs},`,
          "safe_error = 'deferred_lifecycle'",
          `where event_id = ${sqlText3(event.eventId)};`
        ].join(" ")
      );
      return { outcome: "waiting_for_rename" };
    });
  }
  /**
   * Close one content event through the owner-aware terminal exit. The
   * attempt audit, terminal row, multipart/progress cleanup and intent
   * decision share one serialized mutation; queue callers must not compose
   * these writes themselves.
   */
  async resolveIntentAwareContentTerminal(input) {
    if (!isClosedToken2(input.terminalState, JOURNAL_NON_RETRY_EVENT_STATES)) {
      throw journalStoreError("journal_mutation_failed");
    }
    this.#validateIntentAwareAttemptInput(input, input.safeError);
    return this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow((sql) => session.readRows(sql), input.eventId);
      this.#requireNonTerminalEvent(event);
      this.#requireContentEvent(event);
      this.#recordAttemptInSession(session, {
        eventId: input.eventId,
        attemptedAtEpochMs: input.attemptedAtEpochMs,
        outcomeLabel: input.safeError,
        requestCorrelationId: input.requestCorrelationId
      });
      this.#closeContentEventInSession(session, event, input.terminalState, input.safeError);
      const intent = this.#readPendingRenameIntentInSession(session, event.localFileId);
      if (intent === null) {
        return "no_intent";
      }
      const owner = firstRow4(
        session.readRows(
          [
            "select source_id, base_version_id from local_files",
            `where local_file_id = ${sqlText3(event.localFileId)};`
          ].join(" ")
        )
      );
      if (owner === null) {
        throw journalStoreError("journal_image_invalid");
      }
      const [sourceId, baseVersionId] = owner;
      if (sourceId !== null && typeof sourceId !== "string" || baseVersionId !== null && typeof baseVersionId !== "string") {
        throw journalStoreError("journal_image_invalid");
      }
      const pendingSuccessor = firstRow4(
        session.readRows(
          [
            "select 1 from journal_events",
            `where local_file_id = ${sqlText3(event.localFileId)}`,
            `and event_id <> ${sqlText3(event.eventId)}`,
            "and operation in ('create', 'update')",
            "and state in ('queued', 'preflight', 'uploading', 'waiting_retry')",
            "limit 1;"
          ].join(" ")
        )
      );
      if (typeof sourceId === "string" && typeof baseVersionId === "string" || pendingSuccessor !== null) {
        return "intent_preserved";
      }
      this.#reparentAndClearPendingRenameIntentInSession(
        session,
        event.localFileId,
        intent.currentPath,
        false
      );
      return "intent_reparented";
    });
  }
  // --- lifecycle orchestration helpers (child 5) ------------------------------------------------
  /**
   * Freeze every still-pending content event (`queued` / `preflight` /
   * `waiting_retry`) of one tracked file as a terminal `deferred_lifecycle`
   * row, in one transaction. Lifecycle events are never touched: a
   * `rename` / `move` / `delete` / `restore` row already owns its own
   * durable identity and must not be replaced by a content freeze.
   *
   * The lifecycle capture calls this BEFORE recording a rename / move /
   * delete so every later queue pass ignores the file's outstanding
   * content work without ever queuing more.
   */
  async freezePendingForLocalFile(localFileId) {
    if (!isUuid4(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow4(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText3(localFileId)};`
        )
      );
      if (existing === null) {
        throw journalStoreError("journal_mutation_failed");
      }
      const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
      session.exec(
        [
          "update journal_events set",
          "state = 'deferred_lifecycle',",
          "next_eligible_retry_epoch_ms = null,",
          "safe_error = 'deferred_lifecycle',",
          "is_fingerprint_frozen = 1",
          `where local_file_id = ${sqlText3(localFileId)}`,
          `and state in (${pendingStateList})`,
          "and operation in ('create', 'update');"
        ].join(" ")
      );
      session.exec(
        [
          "delete from multipart_upload_progress where event_id in (",
          "select event_id from journal_events",
          `where local_file_id = ${sqlText3(localFileId)}`,
          "and state = 'deferred_lifecycle'",
          "and operation in ('create', 'update'));"
        ].join(" ")
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText3(localFileId)};`
      );
    });
  }
  /**
   * Remove one tracked `local_files` row together with every dependent
   * event / operand row in one transaction. The lifecycle capture calls
   * this AFTER a tombstone event has been recorded, so the durable
   * operand row keeps the tombstone reference for restore even though
   * the local mapping row itself is gone.
   */
  async removeLocalMapping(localFileId) {
    if (!isUuid4(localFileId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = firstRow4(
        session.readRows(
          `select local_file_id from local_files where local_file_id = ${sqlText3(localFileId)};`
        )
      );
      if (existing === null) {
        return;
      }
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText3(localFileId)};`
      );
      session.exec(
        `delete from pending_rename_intents where local_file_id = ${sqlText3(localFileId)};`
      );
      session.exec(
        `delete from journal_attempts where event_id in (select event_id from journal_events where local_file_id = ${sqlText3(localFileId)});`
      );
      session.exec(
        `delete from multipart_upload_progress where event_id in (select event_id from journal_events where local_file_id = ${sqlText3(localFileId)});`
      );
      session.exec(
        `delete from lifecycle_event_operands where event_id in (select event_id from journal_events where local_file_id = ${sqlText3(localFileId)});`
      );
      session.exec(
        `delete from journal_events where local_file_id = ${sqlText3(localFileId)};`
      );
      session.exec(
        `delete from local_files where local_file_id = ${sqlText3(localFileId)};`
      );
    });
  }
  // --- receipts and attempts (spec 6.3, 7.2) ----------------------------------------------------
  /**
   * Persist the canonical receipt of one committed event: the event closes
   * as `committed` and its file takes the server-returned source and base
   * version identities. The observed fingerprint is left untouched — a
   * successor capture may already have observed newer bytes — but the
   * provable `last_committed_*` triple is updated from the event's frozen
   * fingerprint so the lifecycle capture can verify a later restore
   * eligibility against bytes the server actually acknowledged.
   */
  async recordCommittedReceipt(input) {
    if (!isUuid4(input.eventId) || !isUuid4(input.sourceId) || !isUuid4(input.baseVersionId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        input.eventId
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set state = 'committed',",
          "next_eligible_retry_epoch_ms = null, safe_error = null,",
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText3(input.eventId)};`
        ].join(" ")
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText3(input.eventId)};`
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where event_id = ${sqlText3(input.eventId)};`
      );
      session.exec(
        [
          "update local_files set",
          `source_id = ${sqlText3(input.sourceId)},`,
          `base_version_id = ${sqlText3(input.baseVersionId)},`,
          `last_committed_sha256 = ${sqlText3(event.fingerprint.sha256)},`,
          `last_committed_size_bytes = ${event.fingerprint.sizeBytes},`,
          `last_committed_media_type = ${sqlText3(event.fingerprint.mediaType)}`,
          `where local_file_id = ${sqlText3(event.localFileId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Persist the safe no-op receipt of one `no_change` preflight (spec 7.2,
   * 10.1): the event closes as `no_change` and its file adopts the confirmed
   * current server base — no bytes were uploaded and nothing retries. The
   * `last_committed_*` triple is updated from the event's frozen fingerprint
   * so the lifecycle capture can verify restore eligibility against the
   * server's acknowledgement. Any multipart progress clears in the same
   * mutation: a no-change outcome leaves no session work owed.
   */
  async recordNoChangeReceipt(input) {
    if (!isUuid4(input.eventId) || !isUuid4(input.sourceId) || !isUuid4(input.baseVersionId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        input.eventId
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "update journal_events set state = 'no_change',",
          "next_eligible_retry_epoch_ms = null, safe_error = null,",
          "is_fingerprint_frozen = 1",
          `where event_id = ${sqlText3(input.eventId)};`
        ].join(" ")
      );
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText3(input.eventId)};`
      );
      session.exec(
        `delete from pending_rename_intent_missing_file_deferrals where event_id = ${sqlText3(input.eventId)};`
      );
      session.exec(
        [
          "update local_files set",
          `source_id = ${sqlText3(input.sourceId)},`,
          `base_version_id = ${sqlText3(input.baseVersionId)},`,
          `last_committed_sha256 = ${sqlText3(event.fingerprint.sha256)},`,
          `last_committed_size_bytes = ${event.fingerprint.sizeBytes},`,
          `last_committed_media_type = ${sqlText3(event.fingerprint.mediaType)}`,
          `where local_file_id = ${sqlText3(event.localFileId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Append one redacted attempt-audit row and prune the per-event ring to
   * the most recent {@link MAX_EVENT_ATTEMPT_HISTORY} entries inside the
   * same transaction (spec 6.3).
   */
  async recordEventAttempt(input) {
    if (!isUuid4(input.eventId) || !isNonNegativeInteger4(input.attemptedAtEpochMs) || !isClosedToken2(input.outcomeLabel, JOURNAL_SAFE_ERROR_LABELS) || typeof input.requestCorrelationId !== "string" || input.requestCorrelationId.length === 0 || input.requestCorrelationId.length > MAX_REQUEST_CORRELATION_ID_LENGTH) {
      throw journalStoreError("journal_mutation_failed");
    }
    for (const character of input.requestCorrelationId) {
      const codeUnit = character.charCodeAt(0);
      if (codeUnit < 32 || codeUnit > 126) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
    await this.#database.runSerializedMutation((session) => {
      this.#requireEventRow(
        (sql) => session.readRows(sql),
        input.eventId
      );
      session.exec(
        [
          "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
          "request_correlation_id) values (",
          `${sqlText3(input.eventId)}, ${input.attemptedAtEpochMs},`,
          `${sqlText3(input.outcomeLabel)}, ${sqlText3(input.requestCorrelationId)});`
        ].join(" ")
      );
      session.exec(
        [
          "delete from journal_attempts where event_id = ",
          `${sqlText3(input.eventId)} and attempt_ordinal not in (`,
          "select attempt_ordinal from journal_attempts",
          `where event_id = ${sqlText3(input.eventId)}`,
          `order by attempt_ordinal desc limit ${MAX_EVENT_ATTEMPT_HISTORY});`
        ].join(" ")
      );
    });
  }
  // --- multipart safe progress (child 7 spec 4.1, task 9) ----------------------------------------
  /**
   * Persist the SAFE progress of one frozen event's multipart transfer
   * (child 7 spec 4.1): the opaque public session ID, the fixed geometry
   * and expiry of the server plan, the completed part-number set, the last
   * observed closed session state and the last closed retry/status token.
   * The whole record is validated against the closed contract BEFORE any
   * SQL runs — unknown fields, hostile session IDs, out-of-geometry part
   * numbers and foreign tokens never reach SQLite, so no URL, provider
   * identity, staging key or digest can persist. The row lands in the
   * same serialized mutation the event's dispatch state lives in, bound
   * to a known nonterminal event; a terminal event never resurrects
   * progress (its frozen outcome is the durable evidence instead).
   */
  async saveMultipartProgress(record) {
    validateMultipartProgressRecord(record);
    await this.#database.runSerializedMutation((session) => {
      const event = this.#requireEventRow(
        (sql) => session.readRows(sql),
        record.eventId
      );
      this.#requireNonTerminalEvent(event);
      session.exec(
        [
          "insert or replace into multipart_upload_progress (event_id, session_id,",
          "part_size_bytes, part_count, expires_at_epoch_ms, completed_part_numbers_json,",
          "session_state, safe_reason) values (",
          `${sqlText3(record.eventId)}, ${sqlText3(record.sessionId)},`,
          `${record.partSizeBytes}, ${record.partCount}, ${record.expiresAtEpochMs},`,
          `${sqlText3(JSON.stringify(record.completedPartNumbers))},`,
          `${sqlText3(record.sessionState)},`,
          `${record.safeReason === null ? "null" : sqlText3(record.safeReason)});`
        ].join(" ")
      );
    });
  }
  /**
   * Read the durable safe progress bound to one journal event, or null
   * when the event carries no session. A persisted row that violates the
   * closed contract fails closed as image corruption.
   */
  readMultipartProgress(eventId) {
    if (!isUuid4(eventId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow4(
      this.#database.readAll(
        [
          "select session_id, part_size_bytes, part_count, expires_at_epoch_ms,",
          "completed_part_numbers_json, session_state, safe_reason",
          `from multipart_upload_progress where event_id = ${sqlText3(eventId)};`
        ].join(" ")
      )
    );
    return row === null ? null : parseMultipartProgressRow(row, eventId);
  }
  /**
   * Clear the durable safe progress of one event (child 7 spec 4.1): the
   * idempotent exact-key cleanup the runner issues when a session is
   * superseded or its evidence is no longer owed. The journal event
   * itself is never touched — cancellation and interruption retain it.
   */
  async clearMultipartProgress(eventId) {
    if (!isUuid4(eventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      session.exec(
        `delete from multipart_upload_progress where event_id = ${sqlText3(eventId)};`
      );
    });
  }
  /**
   * The closed multipart session-state histogram of the durable safe
   * progress table (multipart task 11): one count per closed
   * `MultipartSessionState`, zero-initialised, aggregated over
   * `multipart_upload_progress` rows only. The read carries no session ID,
   * part geometry, expiry or any other row detail — the closed state token
   * and its count are the whole answer, the input shape the status
   * projection consumes verbatim. A persisted state outside the closed
   * vocabulary is image corruption and fails closed.
   */
  readMultipartSessionStateCounts() {
    const counts = {};
    for (const state of MULTIPART_SESSION_STATES) {
      counts[state] = 0;
    }
    const result = this.#database.readAll(
      "select session_state, count(*) from multipart_upload_progress group by session_state;"
    );
    for (const row of result[0]?.values ?? []) {
      const [state, count] = row;
      if (typeof state !== "string" || !isClosedToken2(state, MULTIPART_SESSION_STATES)) {
        throw journalStoreError("journal_image_invalid");
      }
      if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
        throw journalStoreError("journal_image_invalid");
      }
      counts[state] = count;
    }
    return counts;
  }
  /**
   * The closed set of safe-reason tokens the durable multipart progress
   * rows currently carry (multipart task 11): the distinct non-null
   * `safe_reason` values, each a member of the closed twelve-token
   * vocabulary, in first-observed read order. No row detail travels with
   * the tokens; a foreign token is image corruption and fails closed.
   */
  readMultipartSafeReasonCodes() {
    const codes = /* @__PURE__ */ new Set();
    const result = this.#database.readAll(
      "select distinct safe_reason from multipart_upload_progress where safe_reason is not null;"
    );
    for (const row of result[0]?.values ?? []) {
      const [safeReason] = row;
      if (typeof safeReason !== "string" || !isClosedToken2(safeReason, MULTIPART_SAFE_REASON_TOKENS)) {
        throw journalStoreError("journal_image_invalid");
      }
      codes.add(safeReason);
    }
    return Array.from(codes);
  }
  // --- queries (spec 6.3, 9) -----------------------------------------------------------------------
  /** One event by identity, or null; the shape never includes internals. */
  readEvent(eventId) {
    const row = firstRow4(
      this.#database.readAll(
        selectColumns("journal_events", JOURNAL_EVENT_COLUMNS, `where event_id = ${sqlText3(eventId)}`)
      )
    );
    return row === null ? null : toPublicEvent(parseJournalEventRow(row));
  }
  /**
   * The oldest CONTENT event one queue pass may select (spec 8): the
   * earliest `queued`/`waiting_retry` row whose retry time has passed,
   * where an event left in `preflight`/`uploading` by an interrupted pass
   * stays eligible for the exact same-identity replay of spec 10.3.
   *
   * Lane discipline: rows carrying lifecycle operands (`rename`, `move`,
   * `delete`, `restore`) are NEVER selected here — their placeholder
   * zeros fingerprint belongs to the lifecycle dispatch lane, and a
   * content-lane dispatch of such a row would terminally destroy the
   * lifecycle intent through the content re-fingerprint check. The
   * lifecycle lane selects through its own
   * {@link JournalLifecycleRepository.readOldestEligibleLifecycleEvent}
   * selector; this exclusion is enforced structurally with a `NOT
   * EXISTS` probe on `lifecycle_event_operands` (no schema change).
   */
  readOldestEligibleEvent(nowEpochMs) {
    if (!isNonNegativeInteger4(nowEpochMs)) {
      throw journalStoreError("journal_query_failed");
    }
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const row = firstRow4(
      this.#database.readAll(
        [
          `select ${JOURNAL_EVENT_COLUMNS.join(", ")} from journal_events`,
          `where ((state in (${coalescableStateList})`,
          "and (next_eligible_retry_epoch_ms is null",
          `or next_eligible_retry_epoch_ms <= ${nowEpochMs}))`,
          "or state in ('preflight', 'uploading'))",
          "and not exists (",
          "select 1 from lifecycle_event_operands content_lane_exclusion",
          "where content_lane_exclusion.event_id = journal_events.event_id)",
          "order by created_at_epoch_ms asc, rowid asc limit 1;"
        ].join(" ")
      )
    );
    return row === null ? null : toPublicEvent(parseJournalEventRow(row));
  }
  /** One tracked file by its plugin-local identity, or null. */
  readLocalFileByLocalFileId(localFileId) {
    if (typeof localFileId !== "string" || localFileId.length === 0) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow4(
      this.#database.readAll(
        selectColumns(
          "local_files",
          LOCAL_FILE_COLUMNS,
          `where local_file_id = ${sqlText3(localFileId)}`
        )
      )
    );
    return row === null ? null : parseLocalFileRow(row);
  }
  /** Every event of one file, oldest first — terminal history included. */
  readEventsByLocalFileId(localFileId) {
    const result = this.#database.readAll(
      selectColumns(
        "journal_events",
        JOURNAL_EVENT_COLUMNS,
        `where local_file_id = ${sqlText3(localFileId)} order by created_at_epoch_ms asc, rowid asc`
      )
    );
    return (result[0]?.values ?? []).map((row) => toPublicEvent(parseJournalEventRow(row)));
  }
  /** One tracked file by its normalized current path, or null. */
  readLocalFileByPath(normalizedPath) {
    validateNormalizedPath2(normalizedPath);
    return this.#readLocalFileRow((sql) => this.#database.readAll(sql), normalizedPath);
  }
  /** The bounded attempted-event history of one event, oldest first (redacted). */
  readEventAttemptHistory(eventId) {
    const result = this.#database.readAll(
      selectColumns(
        "journal_attempts",
        JOURNAL_ATTEMPT_COLUMNS,
        `where event_id = ${sqlText3(eventId)} order by attempt_ordinal asc`
      )
    );
    return (result[0]?.values ?? []).map((row) => parseJournalAttemptRow(row));
  }
  /** The number of events that still owe work (the spec-6.4 pending count). */
  countPendingEvents() {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const row = firstRow4(
      this.#database.readAll(
        `select count(*) from journal_events where state in (${pendingStateList});`
      )
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }
  /**
   * The earliest scheduled retry deadline among pending events, or null
   * when no pending event waits on a retry time (a `queued` row is
   * immediately eligible and carries no deadline). The plugin's one-shot
   * scheduled retry trigger uses this deadline — plus a small safety
   * margin — to time the single follow-up pass it arms after a pass ends
   * `retry_scheduled` or `login_required`.
   */
  readEarliestPendingRetryEpochMs() {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const row = firstRow4(
      this.#database.readAll(
        [
          "select min(next_eligible_retry_epoch_ms) from journal_events",
          `where state in (${pendingStateList})`,
          "and next_eligible_retry_epoch_ms is not null;"
        ].join(" ")
      )
    );
    const earliest = row?.[0];
    if (earliest === null || earliest === void 0) {
      return null;
    }
    if (typeof earliest !== "number" || !Number.isInteger(earliest) || earliest < 0) {
      throw journalStoreError("journal_image_invalid");
    }
    return earliest;
  }
  /**
   * The redacted event histogram of the status projection (spec 11): the
   * newest row for each local file, grouped by closed state and closed safe
   * error label. A later capture supersedes every predecessor outcome for
   * current status purposes while immutable audit evidence remains in the
   * journal — never a path, digest, credential or other row detail.
   */
  readEventStateErrorCounts() {
    const result = this.#database.readAll(
      [
        "select current_event.state, current_event.safe_error, count(*) from journal_events current_event",
        "where not exists (",
        "select 1 from journal_events successor_event",
        "where successor_event.local_file_id = current_event.local_file_id",
        "and successor_event.rowid > current_event.rowid",
        ")",
        "group by current_event.state, current_event.safe_error;"
      ].join(" ")
    );
    return (result[0]?.values ?? []).map((row) => {
      const [state, safeError, eventCount] = row;
      if (typeof state !== "string" || !isClosedToken2(state, JOURNAL_EVENT_STATES) || !isNullableText3(safeError) || safeError !== null && !isClosedToken2(safeError, JOURNAL_SAFE_ERROR_LABELS) || typeof eventCount !== "number" || !Number.isInteger(eventCount) || eventCount < 0) {
        throw journalStoreError("journal_image_invalid");
      }
      return {
        state,
        safeError,
        eventCount
      };
    });
  }
  /**
   * The current local-only status of every tracked note, in deterministic
   * normalized-path order. The newest journal event is selected per local
   * file; immutable predecessor events remain available through audit reads
   * but cannot become a present UI blocker or reach aggregate status.
   */
  readLocalNoteSyncStatuses() {
    const reconcileRow = firstRow4(
      this.#database.readAll(
        "select is_reconcile_required from journal_meta where singleton_key = 1;"
      )
    );
    const isReconcileRequired = reconcileRow?.[0];
    if (isReconcileRequired !== 0 && isReconcileRequired !== 1) {
      throw journalStoreError("journal_image_invalid");
    }
    const result = this.#database.readAll(
      [
        "select local_file.normalized_path, local_file.policy_revision,",
        "local_file.observed_sha256, local_file.observed_size_bytes, local_file.observed_media_type,",
        "local_file.last_committed_sha256, local_file.last_committed_size_bytes, local_file.last_committed_media_type,",
        `current_event.${JOURNAL_EVENT_COLUMNS.join(", current_event.")}`,
        "from local_files local_file",
        "left join journal_events current_event on current_event.rowid = (",
        "select candidate_event.rowid from journal_events candidate_event",
        "where candidate_event.local_file_id = local_file.local_file_id",
        "order by candidate_event.rowid desc limit 1",
        ")",
        "order by local_file.normalized_path asc;"
      ].join(" ")
    );
    return (result[0]?.values ?? []).map(
      (row) => parseLocalNoteSyncStatusRow(row, isReconcileRequired === 1)
    );
  }
  /**
   * The redacted lifecycle-state histogram of the status projection
   * (Task 10, spec 6.3): the closed {@link LifecycleLocalFileState} of
   * each tracked `local_files` row, counted per state. The closed enum
   * is the only thing that reaches the status surface — no path,
   * source id, locator, tombstone id, fingerprint or any other row
   * detail ever escapes the read.
   */
  readLifecycleStateCounts() {
    const counts = {
      active: 0,
      rename_pending: 0,
      move_pending: 0,
      delete_pending: 0,
      restore_pending: 0,
      tombstoned: 0,
      restored: 0,
      reconcile_required: 0
    };
    const result = this.#database.readAll(
      "select lifecycle_state, count(*) from local_files group by lifecycle_state;"
    );
    for (const row of result[0]?.values ?? []) {
      const [state, count] = row;
      if (typeof state !== "string" || !isClosedToken2(state, LIFECYCLE_LOCAL_FILE_STATES)) {
        throw journalStoreError("journal_image_invalid");
      }
      if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
        throw journalStoreError("journal_image_invalid");
      }
      counts[state] = count;
    }
    return counts;
  }
  /**
   * The number of lifecycle events that still owe work (Task 10): the
   * oldest eligible lifecycle event count surfaced as a status
   * affordance. The number is derived from the same closed pending-event
   * vocabulary the content queue uses, restricted to the four lifecycle
   * operations so the count never leaks a content event.
   */
  countPendingLifecycleEvents() {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const row = firstRow4(
      this.#database.readAll(
        [
          `select count(*) from journal_events`,
          `where state in (${pendingStateList})`,
          `and operation in ('rename', 'move', 'delete', 'restore');`
        ].join(" ")
      )
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }
  /**
   * The number of failed attempts in the bounded `journal_attempts`
   * ring (Task 10, spec 6.3): every row whose closed `outcome_label`
   * is anything other than the success token (`committed`) counts as
   * one failed attempt. The number never leaks a path, digest, source
   * id or credential; only closed labels and correlation IDs reach
   * the audit ring.
   */
  countFailedAttempts() {
    const row = firstRow4(
      this.#database.readAll(
        "select count(*) from journal_attempts where outcome_label != 'committed';"
      )
    );
    const count = row?.[0];
    return typeof count === "number" ? count : 0;
  }
  /**
   * The redacted lifecycle blocked-reason-code list of the status
   * projection (Task 10, spec 6.3): the closed set of reasons any
   * lifecycle event currently owns that block its forward progress.
   * The mapping is derived from the existing telemetry (event states
   * + bounded attempt outcome labels); no path, digest, locator,
   * source id, tombstone id or credential ever escapes the read.
   */
  readLifecycleBlockedReasonCodes() {
    const codes = /* @__PURE__ */ new Set();
    const blockedEvents = this.#database.readAll(
      [
        "select safe_error from journal_events",
        "where operation in ('rename', 'move', 'delete', 'restore')",
        "and safe_error = 'integrity_failed';"
      ].join(" ")
    );
    for (const row of blockedEvents[0]?.values ?? []) {
      const [safeError] = row;
      if (typeof safeError === "string") {
        codes.add(safeError);
      }
    }
    return Array.from(codes);
  }
  /**
   * The retained restorable rows the explicit restore surface addresses
   * (Task 10, spec 6.3 + 7.1; the explicit-restore target reservation
   * spec extends the set). The read returns every tracked file row that
   * still holds an open tombstone — `tombstoned`, or `restore_pending`
   * (a durable reservation or an in-flight restore event the operator
   * can resume) — identified by the plugin-local `localFileId`. No path,
   * source id, tombstone id, locator or fingerprint reaches the caller;
   * the picker constructs its display label from the safe identifier
   * alone.
   */
  readRestorableLocalFileIds() {
    const rows = this.#database.readAll(
      [
        "select local_file_id from local_files",
        "where lifecycle_state in ('tombstoned', 'restore_pending')",
        "and open_tombstone_id is not null",
        "order by normalized_path asc;"
      ].join(" ")
    );
    return (rows[0]?.values ?? []).map((row) => row[0]).filter((value) => typeof value === "string" && value.length > 0);
  }
  // --- internals ------------------------------------------------------------------------------------
  #validateIntentAwareAttemptInput(input, outcomeLabel) {
    if (!isUuid4(input.eventId) || !isNonNegativeInteger4(input.attemptedAtEpochMs) || !isClosedToken2(outcomeLabel, JOURNAL_SAFE_ERROR_LABELS) || typeof input.requestCorrelationId !== "string" || input.requestCorrelationId.length === 0 || input.requestCorrelationId.length > MAX_REQUEST_CORRELATION_ID_LENGTH || input.nextEligibleRetryEpochMs !== void 0 && !isNonNegativeInteger4(input.nextEligibleRetryEpochMs)) {
      throw journalStoreError("journal_mutation_failed");
    }
    for (const character of input.requestCorrelationId) {
      const codeUnit = character.charCodeAt(0);
      if (codeUnit < 32 || codeUnit > 126) {
        throw journalStoreError("journal_mutation_failed");
      }
    }
  }
  #recordAttemptInSession(session, input) {
    session.exec(
      [
        "insert into journal_attempts (event_id, attempted_at_epoch_ms, outcome_label,",
        "request_correlation_id) values (",
        `${sqlText3(input.eventId)}, ${input.attemptedAtEpochMs},`,
        `${sqlText3(input.outcomeLabel)}, ${sqlText3(input.requestCorrelationId)});`
      ].join(" ")
    );
    session.exec(
      [
        "delete from journal_attempts where event_id = ",
        `${sqlText3(input.eventId)} and attempt_ordinal not in (`,
        "select attempt_ordinal from journal_attempts",
        `where event_id = ${sqlText3(input.eventId)}`,
        `order by attempt_ordinal desc limit ${MAX_EVENT_ATTEMPT_HISTORY});`
      ].join(" ")
    );
  }
  #requireContentEvent(event) {
    if (event.operation !== "create" && event.operation !== "update") {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  #closeContentEventInSession(session, event, terminalState, safeError) {
    session.exec(
      [
        "update journal_events set",
        `state = ${sqlText3(terminalState)},`,
        "next_eligible_retry_epoch_ms = null,",
        `safe_error = ${sqlText3(safeError)},`,
        "is_fingerprint_frozen = 1",
        `where event_id = ${sqlText3(event.eventId)};`
      ].join(" ")
    );
    session.exec(
      `delete from multipart_upload_progress where event_id = ${sqlText3(event.eventId)};`
    );
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where event_id = ${sqlText3(event.eventId)};`
    );
  }
  #readPendingRenameIntentInSession(session, localFileId) {
    const row = firstRow4(
      session.readRows(
        [
          "select prior_path, current_path from pending_rename_intents",
          `where local_file_id = ${sqlText3(localFileId)};`
        ].join(" ")
      )
    );
    if (row === null) {
      return null;
    }
    const [priorPath, currentPath] = row;
    if (typeof priorPath !== "string" || priorPath.length === 0 || typeof currentPath !== "string" || currentPath.length === 0) {
      throw journalStoreError("journal_image_invalid");
    }
    return { priorPath, currentPath };
  }
  #reparentAndClearPendingRenameIntentInSession(session, localFileId, currentPath, requiresReconciliation) {
    session.exec(
      `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText3(localFileId)};`
    );
    session.exec(
      `delete from pending_rename_intents where local_file_id = ${sqlText3(localFileId)};`
    );
    session.exec(
      [
        "update local_files set",
        `normalized_path = ${sqlText3(currentPath)},`,
        ...requiresReconciliation ? ["lifecycle_state = 'reconcile_required',", "open_tombstone_id = null"] : ["lifecycle_state = 'active',", "open_tombstone_id = null"],
        `where local_file_id = ${sqlText3(localFileId)};`
      ].join(" ")
    );
    if (requiresReconciliation) {
      this.#persistReconcileRequired(session);
    }
  }
  #readLocalFileRow(read, normalizedPath) {
    const row = firstRow4(
      read(
        selectColumns(
          "local_files",
          LOCAL_FILE_COLUMNS,
          `where normalized_path = ${sqlText3(normalizedPath)}`
        )
      )
    );
    return row === null ? null : parseLocalFileRow(row);
  }
  #requireLocalFileRow(read, normalizedPath) {
    const localFile = this.#readLocalFileRow(read, normalizedPath);
    if (localFile === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    return localFile;
  }
  #requireEventRow(read, eventId) {
    const row = firstRow4(
      read(selectColumns("journal_events", JOURNAL_EVENT_COLUMNS, `where event_id = ${sqlText3(eventId)}`))
    );
    if (row === null) {
      throw journalStoreError("journal_mutation_failed");
    }
    return parseJournalEventRow(row);
  }
  #requireNonTerminalEvent(event) {
    if (!isClosedToken2(event.state, JOURNAL_PENDING_EVENT_STATES)) {
      throw journalStoreError("journal_mutation_failed");
    }
  }
  /** The newest same-file event still replaceable before preflight (spec 7.2). */
  #readCoalescableEventRow(session, localFileId) {
    const coalescableStateList = JOURNAL_COALESCABLE_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const coalescableOperationList = ["create", "update"].map((operation) => sqlText3(operation)).join(", ");
    const lifecycleProbe = firstRow4(
      session.readRows(
        `select 1 from journal_events where local_file_id = ${sqlText3(localFileId)} and operation in ('rename', 'move', 'delete', 'restore') limit 1;`
      )
    );
    if (lifecycleProbe !== null) {
      return null;
    }
    const row = firstRow4(
      session.readRows(
        selectColumns(
          "journal_events",
          JOURNAL_EVENT_COLUMNS,
          [
            `where local_file_id = ${sqlText3(localFileId)}`,
            `and state in (${coalescableStateList})`,
            `and operation in (${coalescableOperationList})`,
            "and is_fingerprint_frozen = 0",
            "order by created_at_epoch_ms desc, rowid desc limit 1"
          ].join(" ")
        )
      )
    );
    return row === null ? null : parseJournalEventRow(row);
  }
  /** Whether either spec-6.4 soft limit is reached inside this transaction. */
  #isAtQueueLimit(session) {
    const pendingStateList = JOURNAL_PENDING_EVENT_STATES.map((state) => sqlText3(state)).join(", ");
    const pendingRow = firstRow4(
      session.readRows(
        `select count(*) from journal_events where state in (${pendingStateList});`
      )
    );
    const pageCountRow = firstRow4(session.readRows("pragma page_count;"));
    const pageSizeRow = firstRow4(session.readRows("pragma page_size;"));
    const pendingCount = pendingRow?.[0];
    const pageCount = pageCountRow?.[0];
    const pageSize = pageSizeRow?.[0];
    if (typeof pendingCount !== "number" || typeof pageCount !== "number" || typeof pageSize !== "number") {
      throw journalStoreError("journal_query_failed");
    }
    return pendingCount >= MAX_PENDING_EVENTS || pageCount * pageSize >= MAX_JOURNAL_SIZE_BYTES;
  }
  /** Durably flag the journal for child-6 reconciliation (spec 6.4, 12). */
  #persistReconcileRequired(session) {
    const meta = session.readJournalMeta();
    if (!meta.isReconcileRequired) {
      session.writeJournalMeta({ ...meta, isReconcileRequired: true });
    }
  }
  /** Refresh only the observed fingerprint columns of one tracked file. */
  #writeObservedFingerprint(exec, localFileId, input) {
    exec(
      [
        "update local_files set",
        `observed_sha256 = ${sqlText3(input.fingerprint.sha256)},`,
        `observed_size_bytes = ${input.fingerprint.sizeBytes},`,
        `observed_media_type = ${sqlText3(input.fingerprint.mediaType)},`,
        `policy_revision = ${input.policyRevisionNumber}`,
        `where local_file_id = ${sqlText3(localFileId)};`
      ].join(" ")
    );
  }
};

// src/journal/sync-api.ts
var SYNC_API_FAILURE_KINDS = [
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "access_expired",
  "login_required",
  "blocked_size",
  "blocked_conflict",
  "integrity_failed",
  "policy_denied",
  "operation_retry_required"
];
var SYNC_API_ENVELOPE_ERROR_CODES = [
  "device_credential_invalid",
  "authorization_scope_denied",
  "authentication_rate_limited",
  "internal_error",
  "database_connection_unavailable",
  "exclusion_policy_denied",
  "exclusion_policy_not_initialized",
  "exclusion_policy_signing_unavailable",
  "small_file_preflight_invalid",
  "small_file_size_limit_exceeded",
  "source_locator_conflict",
  "small_file_content_integrity_failed",
  "small_file_operation_identity_mismatch",
  "small_file_operation_not_found",
  "small_file_operation_expired",
  "small_file_upload_state_invalid",
  // The closed multipart registry block (child 7 spec 7) the multipart
  // surface consumes.
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid",
  "multipart_part_invalid",
  "multipart_part_url_rejected",
  "multipart_provider_state_invalid",
  "multipart_completion_in_progress",
  "multipart_integrity_failed",
  "multipart_policy_denied",
  "multipart_cleanup_failed",
  "multipart_local_content_changed",
  "multipart_dependency_unavailable"
];
var SyncApiError = class extends Error {
  kind;
  canResumeClaimedOperation;
  requestId;
  wireErrorCode;
  isCredentialAbsent;
  constructor(kind, canResumeClaimedOperation = false, requestId = null, wireErrorCode = null, isCredentialAbsent = false) {
    super(`sync api failed: ${kind}`);
    this.name = "SyncApiError";
    this.kind = kind;
    this.canResumeClaimedOperation = canResumeClaimedOperation;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
    this.isCredentialAbsent = isCredentialAbsent;
  }
};
function syncApiError(kind, canResumeClaimedOperation = false, requestId = null, wireErrorCode = null, isCredentialAbsent = false) {
  return new SyncApiError(kind, canResumeClaimedOperation, requestId, wireErrorCode, isCredentialAbsent);
}
function buildJournalEventWireBody(input) {
  return {
    event_id: input.eventId,
    idempotency_key: input.idempotencyKey,
    operation: input.operation,
    local_file_id: input.localFileId,
    ...input.sourceId === null ? {} : { source_id: input.sourceId },
    ...input.baseVersionId === null ? {} : { base_version_id: input.baseVersionId },
    normalized_locator: input.normalizedLocator,
    sha256: input.fingerprint.sha256,
    size_bytes: input.fingerprint.sizeBytes,
    media_type: input.fingerprint.mediaType,
    policy_revision: input.policyRevisionNumber
  };
}
var MULTIPART_SESSION_ID_WIRE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
function isMultipartSessionIdWireShape(value) {
  return typeof value === "string" && MULTIPART_SESSION_ID_WIRE_PATTERN.test(value) && !isUuid4(value);
}
function parseExpiresAtEpochMs(value) {
  if (typeof value !== "string") {
    throw syncApiError("server_error");
  }
  const expiresAtEpochMs = Date.parse(value);
  if (!Number.isFinite(expiresAtEpochMs) || expiresAtEpochMs < 0) {
    throw syncApiError("server_error");
  }
  return expiresAtEpochMs;
}
function parseMultipartGeometry(data) {
  const { session_id: sessionId, part_count: partCount, part_size_bytes: partSizeBytes } = data;
  if (!isMultipartSessionIdWireShape(sessionId) || typeof partCount !== "number" || !Number.isInteger(partCount) || partCount < 1 || partCount > MAX_MULTIPART_PART_COUNT || typeof partSizeBytes !== "number" || !Number.isInteger(partSizeBytes) || partSizeBytes !== MULTIPART_PART_SIZE_BYTES) {
    throw syncApiError("server_error");
  }
  return {
    sessionId,
    partCount,
    partSizeBytes,
    expiresAtEpochMs: parseExpiresAtEpochMs(data["expires_at"])
  };
}
function parseMultipartSessionState(value) {
  if (typeof value !== "string" || !MULTIPART_SESSION_STATES.includes(value)) {
    throw syncApiError("server_error");
  }
  return value;
}
function parseMultipartTerminalResult(value) {
  if (value === null || value === void 0) {
    return null;
  }
  if (!isRecord2(value)) {
    throw syncApiError("server_error");
  }
  const resultKind = value["result_kind"];
  if (resultKind !== "committed" && resultKind !== "no_change") {
    throw syncApiError("server_error");
  }
  return { resultKind, ...parseTerminalReceipt(value) };
}
function parseMultipartSessionStatus(data) {
  if (!isRecord2(data)) {
    throw syncApiError("server_error");
  }
  const geometry = parseMultipartGeometry(data);
  const completedPartNumbers = data["completed_part_numbers"];
  if (!Array.isArray(completedPartNumbers)) {
    throw syncApiError("server_error");
  }
  const seenPartNumbers = /* @__PURE__ */ new Set();
  for (const partNumber of completedPartNumbers) {
    if (typeof partNumber !== "number" || !Number.isInteger(partNumber) || partNumber < 1 || partNumber > geometry.partCount || seenPartNumbers.has(partNumber)) {
      throw syncApiError("server_error");
    }
    seenPartNumbers.add(partNumber);
  }
  return {
    ...geometry,
    state: parseMultipartSessionState(data["state"]),
    completedPartNumbers: [...seenPartNumbers].sort((left, right) => left - right),
    terminalResult: parseMultipartTerminalResult(data["terminal_result"])
  };
}
function parseMultipartPartUrl(data, requestedPartNumber) {
  if (!isRecord2(data)) {
    throw syncApiError("server_error");
  }
  const { url, part_number: partNumber, offset_bytes: offsetBytes, size_bytes: sizeBytes } = data;
  if (typeof url !== "string" || !(url.startsWith("https://") || url.startsWith("http://")) || partNumber !== requestedPartNumber || typeof offsetBytes !== "number" || !Number.isInteger(offsetBytes) || offsetBytes < 0 || typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 1) {
    throw syncApiError("server_error");
  }
  return {
    url,
    partNumber,
    offsetBytes,
    sizeBytes,
    expiresAtEpochMs: parseExpiresAtEpochMs(data["expires_at"])
  };
}
function parseMultipartCompletion(data) {
  if (!isRecord2(data)) {
    throw syncApiError("server_error");
  }
  const state = parseMultipartSessionState(data["state"]);
  const terminalReceipt = parseMultipartTerminalResult(data["terminal_result"]);
  if (state !== "committed" && terminalReceipt !== null) {
    throw syncApiError("server_error");
  }
  if (state === "committed" && terminalReceipt === null) {
    throw syncApiError("server_error");
  }
  return { state, terminalReceipt };
}
var UUID_PATTERN6 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var OPERATION_TOKEN_PATTERN2 = /^[A-Za-z0-9_-]{32,128}$/;
function parseEnvelopeRequestId(value) {
  return typeof value === "string" && UUID_PATTERN6.test(value) ? value : null;
}
function parseEnvelope2(status, bodyText) {
  let parsed;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure(status, null, null);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw mapWireFailure(status, null, null);
  }
  const envelope = parsed;
  const requestId = parseEnvelopeRequestId(envelope.request_id);
  if (envelope.error !== null && envelope.error !== void 0) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : null;
    throw mapWireFailure(status, code, requestId);
  }
  if (envelope.data === null || envelope.data === void 0) {
    throw mapWireFailure(status, null, requestId);
  }
  return { data: envelope.data, requestId };
}
function mapWireFailure(status, code, requestId) {
  switch (code) {
    case "multipart_session_not_found":
    case "multipart_session_expired":
    case "multipart_session_state_invalid":
    case "multipart_completion_in_progress":
      return syncApiError("operation_retry_required", false, requestId, code);
    case "multipart_part_url_rejected":
    case "multipart_part_invalid":
    case "multipart_cleanup_failed":
    case "multipart_dependency_unavailable":
      return syncApiError("server_error", false, requestId, code);
    case "multipart_provider_state_invalid":
    case "multipart_integrity_failed":
      return syncApiError("integrity_failed", false, requestId, code);
    case "multipart_policy_denied":
      return syncApiError("policy_denied", false, requestId, code);
    default:
      break;
  }
  if (status === 401) {
    return syncApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    return code === null ? syncApiError("server_error", false, requestId, code) : syncApiError("login_required", false, requestId, code);
  }
  if (status === 429) {
    return syncApiError("network_rate_limited", false, requestId, code);
  }
  switch (code) {
    case "small_file_size_limit_exceeded":
      return syncApiError("blocked_size", false, requestId, code);
    case "source_locator_conflict":
      return syncApiError("blocked_conflict", false, requestId, code);
    case "small_file_content_integrity_failed":
    case "small_file_operation_identity_mismatch":
      return syncApiError("integrity_failed", false, requestId, code);
    case "small_file_operation_not_found":
    case "small_file_operation_expired":
      return syncApiError("operation_retry_required", false, requestId, code);
    case "small_file_upload_state_invalid":
      return syncApiError("operation_retry_required", true, requestId, code);
    default:
      return syncApiError("server_error", false, requestId, code);
  }
}
function isRecord2(value) {
  return typeof value === "object" && value !== null;
}
function parseTerminalReceipt(result) {
  if (!isRecord2(result)) {
    throw syncApiError("server_error");
  }
  const { source_id: sourceId, source_version_id: sourceVersionId, content_version: contentVersion } = result;
  if (typeof sourceId !== "string" || !UUID_PATTERN6.test(sourceId) || typeof sourceVersionId !== "string" || !UUID_PATTERN6.test(sourceVersionId) || typeof contentVersion !== "number" || !Number.isInteger(contentVersion) || contentVersion < 1) {
    throw syncApiError("server_error");
  }
  return { sourceId, sourceVersionId, contentVersion };
}
function createJournalSyncApi(options) {
  const { transport, resolveOrigin, getAccessToken } = options;
  let lastEnvelopeRequestId = null;
  function requireAccessToken() {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      throw syncApiError("login_required", false, null, null, true);
    }
    return accessToken;
  }
  async function perform(request) {
    let response;
    try {
      response = await transport(request);
    } catch {
      throw syncApiError("network_offline");
    }
    try {
      const parsed = parseEnvelope2(response.status, response.bodyText);
      lastEnvelopeRequestId = parsed.requestId;
      return parsed;
    } catch (error) {
      lastEnvelopeRequestId = error instanceof SyncApiError ? error.requestId : null;
      throw error;
    }
  }
  return {
    readLastEnvelopeRequestId: () => lastEnvelopeRequestId,
    async preflightJournalEvent(input) {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/sync/journal-events/preflight`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/json",
          accept: "application/json"
        },
        body: JSON.stringify(buildJournalEventWireBody(input))
      });
      if (!isRecord2(data) || typeof data["outcome"] !== "string") {
        throw syncApiError("server_error");
      }
      switch (data["outcome"]) {
        case "single_part_upload": {
          const operationId = data["operation_id"];
          if (typeof operationId !== "string" || !OPERATION_TOKEN_PATTERN2.test(operationId)) {
            throw syncApiError("server_error");
          }
          return { outcome: "single_part_upload", operationId };
        }
        case "multipart_upload":
          return { outcome: "multipart_upload" };
        case "committed_replay":
          return { outcome: "committed_replay", receipt: parseTerminalReceipt(data["result"]) };
        case "no_change":
          return { outcome: "no_change", receipt: parseTerminalReceipt(data["result"]) };
        case "excluded":
          return { outcome: "excluded" };
        case "conflict": {
          const operationId = data["operation_id"];
          const conflictId = data["conflict_id"];
          const hasGrant = typeof operationId === "string" && OPERATION_TOKEN_PATTERN2.test(operationId);
          const hasConflict = typeof conflictId === "string" && UUID_PATTERN6.test(conflictId);
          if (!hasGrant && operationId !== void 0 && operationId !== null) {
            throw syncApiError("server_error");
          }
          if (!hasConflict && conflictId !== void 0 && conflictId !== null) {
            throw syncApiError("server_error");
          }
          return {
            outcome: "conflict",
            operationId: hasGrant ? operationId : null,
            conflictId: hasConflict ? conflictId : null
          };
        }
        default:
          throw syncApiError("server_error");
      }
    },
    async uploadSmallFileContent(input) {
      const accessToken = requireAccessToken();
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength
      );
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/${encodeURIComponent(input.operationId)}/content`,
        method: "PUT",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/octet-stream",
          accept: "application/json"
        },
        body
      });
      if (!isRecord2(data) || data["result_kind"] !== "committed") {
        throw syncApiError("server_error");
      }
      return parseTerminalReceipt(data);
    },
    async uploadSmallFileConflictCandidate(input) {
      const accessToken = requireAccessToken();
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength
      );
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/${encodeURIComponent(input.operationId)}/conflict-content`,
        method: "PUT",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/octet-stream",
          accept: "application/json"
        },
        body
      });
      if (!isRecord2(data)) {
        throw syncApiError("server_error");
      }
      const conflictId = data["conflict_id"];
      if (typeof conflictId !== "string" || !UUID_PATTERN6.test(conflictId)) {
        throw syncApiError("server_error");
      }
      return { conflictId };
    },
    async createMultipartUploadSession(input) {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/json",
          accept: "application/json"
        },
        body: JSON.stringify(buildJournalEventWireBody(input))
      });
      if (!isRecord2(data)) {
        throw syncApiError("server_error");
      }
      return parseMultipartGeometry(data);
    },
    async getMultipartUploadSession(sessionId) {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(sessionId)}`,
        method: "GET",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json"
        }
      });
      return parseMultipartSessionStatus(data);
    },
    async issueMultipartPartUrl(input) {
      const accessToken = requireAccessToken();
      if (!Number.isInteger(input.partNumber) || input.partNumber < 1) {
        throw syncApiError("server_error");
      }
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          input.sessionId
        )}/parts/${input.partNumber}/url`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json"
        }
      });
      return parseMultipartPartUrl(data, input.partNumber);
    },
    async putMultipartPartBytes(input) {
      const body = input.contentBytes.buffer.slice(
        input.contentBytes.byteOffset,
        input.contentBytes.byteOffset + input.contentBytes.byteLength
      );
      let response;
      try {
        response = await transport({
          url: input.url,
          method: "PUT",
          headers: { "content-type": "application/octet-stream" },
          body
        });
      } catch {
        throw syncApiError("network_offline");
      }
      if (response.status >= 200 && response.status < 300) {
        return "uploaded";
      }
      if (response.status === 401 || response.status === 403) {
        return "url_rejected";
      }
      throw syncApiError("server_error");
    },
    async completeMultipartUploadSession(sessionId) {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          sessionId
        )}/complete`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json"
        }
      });
      return parseMultipartCompletion(data);
    },
    async abortMultipartUploadSession(sessionId) {
      const accessToken = requireAccessToken();
      const { data } = await perform({
        url: `${resolveOrigin()}/api/uploads/multipart-sessions/${requireMultipartSessionIdPath(
          sessionId
        )}/abort`,
        method: "POST",
        headers: {
          authorization: `Bearer ${accessToken}`,
          accept: "application/json"
        }
      });
      return parseMultipartSessionStatus(data);
    }
  };
  function requireMultipartSessionIdPath(sessionId) {
    if (!isMultipartSessionIdWireShape(sessionId)) {
      throw syncApiError("server_error");
    }
    return encodeURIComponent(sessionId);
  }
}

// src/journal/multipart-upload.ts
var MULTIPART_DESKTOP_PART_CONCURRENCY = 3;
var MULTIPART_MOBILE_PART_CONCURRENCY = 2;
function multipartPartConcurrency(platform) {
  return platform === "mobile" ? MULTIPART_MOBILE_PART_CONCURRENCY : MULTIPART_DESKTOP_PART_CONCURRENCY;
}
var PartPutSemaphore = class {
  #limit;
  #activeCount = 0;
  #waiters = [];
  constructor(limit) {
    this.#limit = limit;
  }
  async acquire() {
    if (this.#activeCount >= this.#limit) {
      await new Promise((resolve) => {
        this.#waiters.push(resolve);
      });
    }
    this.#activeCount += 1;
  }
  release() {
    this.#activeCount -= 1;
    this.#waiters.shift()?.();
  }
};
var SESSION_GONE_WIRE_CODES = /* @__PURE__ */ new Set([
  "multipart_session_not_found",
  "multipart_session_expired",
  "multipart_session_state_invalid"
]);
function raceRequestTimeout(request, timeoutMs) {
  return new Promise((resolve, reject) => {
    let hasSettled = false;
    const timeoutHandle = setTimeout(() => {
      if (hasSettled) {
        return;
      }
      hasSettled = true;
      reject(new SyncApiError("network_timeout"));
    }, timeoutMs);
    request.then(
      (value) => {
        if (hasSettled) {
          return;
        }
        hasSettled = true;
        clearTimeout(timeoutHandle);
        resolve(value);
      },
      (error) => {
        if (hasSettled) {
          return;
        }
        hasSettled = true;
        clearTimeout(timeoutHandle);
        reject(error);
      }
    );
  });
}
function multipartFailureTokensOf(error) {
  if (error instanceof SyncApiError) {
    if (error.wireErrorCode !== null && MULTIPART_SAFE_REASON_TOKENS.includes(error.wireErrorCode)) {
      return [error.wireErrorCode];
    }
    return [error.kind];
  }
  if (error instanceof JournalStoreError) {
    return [error.reason];
  }
  return ["reason_unknown"];
}
var MultipartUploadRunner = class {
  #repository;
  #syncApi;
  #fileBytesReader;
  #nowEpochMs;
  #requestTimeoutMs;
  #diagnosticTrail;
  constructor(options) {
    this.#repository = options.repository;
    this.#syncApi = options.syncApi;
    this.#fileBytesReader = options.fileBytesReader;
    this.#nowEpochMs = options.nowEpochMs;
    this.#requestTimeoutMs = options.requestTimeoutMs;
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
  }
  /**
   * Resume or open the frozen event's multipart session and drive only its
   * unfinished parts. `platform` selects the part-PUT semaphore limit and
   * nothing else. Every failure that leaves this boundary appends one
   * closed `multipart_failure` trail entry — the phase's closed stage
   * token plus the failure's closed reason token — before rethrowing, so
   * no catch discards its causal reason.
   */
  async run(event, platform, context = {}) {
    const stageRef = { current: "multipart_resume" };
    try {
      return await this.#run(event, platform, context, stageRef);
    } catch (error) {
      if (error instanceof SyncApiError && error.wireErrorCode !== null && SESSION_GONE_WIRE_CODES.has(error.wireErrorCode)) {
        await this.#repository.clearMultipartProgress(event.eventId).catch(
          (clearError) => {
            this.#recordMultipartFailure(
              "multipart_cleanup",
              multipartFailureTokensOf(clearError)
            );
          }
        );
      }
      this.#recordMultipartFailure(stageRef.current, multipartFailureTokensOf(error));
      throw error;
    }
  }
  async #run(event, platform, context, stageRef) {
    this.#throwIfSuspended(context.signal);
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      return { outcome: "local_file_missing" };
    }
    const persisted = this.#repository.readMultipartProgress(event.eventId);
    const stopState = {
      hasContentChanged: false,
      deadlineReached: false,
      firstFailure: null
    };
    const completedPartNumbers = /* @__PURE__ */ new Set();
    let plan;
    let safeReason;
    if (persisted === null) {
      plan = await this.#request(() => this.#syncApi.createMultipartUploadSession(
        this.#createSessionInput(event, localFile)
      ));
      safeReason = null;
      await this.#persistProgress(event, plan, completedPartNumbers, "created", safeReason);
    } else {
      const status = await this.#request(
        () => this.#syncApi.getMultipartUploadSession(persisted.sessionId)
      );
      if (status.sessionId !== persisted.sessionId || status.partCount !== persisted.partCount || status.partSizeBytes !== persisted.partSizeBytes) {
        throw new SyncApiError("server_error");
      }
      if (status.terminalResult !== null) {
        return terminalOutcomeOf(status.terminalResult);
      }
      await this.#applySessionVerdict(event.eventId, status.state);
      plan = status;
      safeReason = null;
      for (const partNumber of persisted.completedPartNumbers) {
        completedPartNumbers.add(partNumber);
      }
      for (const partNumber of status.completedPartNumbers) {
        completedPartNumbers.add(partNumber);
      }
      await this.#persistProgress(event, plan, completedPartNumbers, status.state, safeReason);
    }
    stageRef.current = "multipart_verify";
    const unfinishedPartNumbers = [];
    for (let partNumber = 1; partNumber <= plan.partCount; partNumber += 1) {
      if (!completedPartNumbers.has(partNumber)) {
        unfinishedPartNumbers.push(partNumber);
      }
    }
    if (unfinishedPartNumbers.length > 0) {
      const semaphore = new PartPutSemaphore(multipartPartConcurrency(platform));
      const workers = unfinishedPartNumbers.map(
        (partNumber) => this.#uploadOnePart({
          event,
          localFile,
          plan,
          partNumber,
          semaphore,
          stopState,
          completedPartNumbers,
          safeReasonRef: { current: safeReason },
          context
        })
      );
      await Promise.allSettled(workers);
      if (stopState.hasContentChanged) {
        return await this.#stopForLocalContentChange(event, plan, completedPartNumbers);
      }
      if (stopState.firstFailure !== null) {
        throw stopState.firstFailure;
      }
      this.#throwIfSuspended(context.signal);
      if (this.#isPastDeadline(context.passDeadlineEpochMs)) {
        stopState.deadlineReached = true;
        return { outcome: "pass_deadline_reached" };
      }
    }
    this.#throwIfSuspended(context.signal);
    if (this.#isPastDeadline(context.passDeadlineEpochMs)) {
      stopState.deadlineReached = true;
      return { outcome: "pass_deadline_reached" };
    }
    const completionCheck = await this.#checkFrozenFile(event, localFile);
    if (completionCheck.kind === "changed") {
      return await this.#stopForLocalContentChange(event, plan, completedPartNumbers);
    }
    if (completionCheck.kind === "missing") {
      return { outcome: "local_file_missing" };
    }
    await this.#persistProgress(event, plan, completedPartNumbers, "completing", null);
    const completion = await this.#request(
      () => this.#syncApi.completeMultipartUploadSession(plan.sessionId)
    );
    if (completion.state === "committed") {
      if (completion.terminalReceipt === null) {
        throw new SyncApiError("server_error");
      }
      return terminalOutcomeOf(completion.terminalReceipt);
    }
    await this.#applySessionVerdict(event.eventId, completion.state);
    throw new SyncApiError("server_error");
  }
  // --- one part -----------------------------------------------------------------------------------
  /**
   * Upload exactly one unfinished part: re-check the frozen file, then
   * under the semaphore request ONE URL, PUT the exact derived range once
   * and immediately discard the response object. A rejected URL
   * reconciles through status and exactly one replacement URL (spec 6.2).
   * The worker never rejects: the first failure lands in the shared stop
   * state and ends the run after every sibling joined.
   */
  async #uploadOnePart(input) {
    const { event, localFile, plan, partNumber, semaphore, stopState, completedPartNumbers, safeReasonRef, context } = input;
    try {
      if (this.#isRunStopped(stopState, context)) {
        return;
      }
      const check = await this.#checkFrozenFile(event, localFile);
      if (check.kind === "changed") {
        stopState.hasContentChanged = true;
        return;
      }
      if (check.kind === "missing") {
        throw new SyncApiError("server_error");
      }
      const fileBytes = check.bytes;
      await semaphore.acquire();
      try {
        if (this.#isRunStopped(stopState, context)) {
          return;
        }
        if (this.#isPastDeadline(context.passDeadlineEpochMs)) {
          stopState.deadlineReached = true;
          return;
        }
        const authorization = await this.#request(
          () => this.#syncApi.issueMultipartPartUrl({
            sessionId: plan.sessionId,
            partNumber
          })
        );
        this.#validatePartRange(authorization, partNumber, fileBytes.byteLength);
        const firstPut = await this.#request(
          () => this.#syncApi.putMultipartPartBytes({
            url: authorization.url,
            contentBytes: fileBytes.subarray(
              authorization.offsetBytes,
              authorization.offsetBytes + authorization.sizeBytes
            )
          })
        );
        if (firstPut === "uploaded") {
          await this.#recordPartCompletion(event, plan, completedPartNumbers, partNumber, safeReasonRef.current);
          return;
        }
        const status = await this.#request(
          () => this.#syncApi.getMultipartUploadSession(plan.sessionId)
        );
        for (const observedPartNumber of status.completedPartNumbers) {
          completedPartNumbers.add(observedPartNumber);
        }
        await this.#persistProgress(
          event,
          plan,
          completedPartNumbers,
          status.state,
          safeReasonRef.current
        );
        if (status.terminalResult !== null) {
          return;
        }
        if (completedPartNumbers.has(partNumber)) {
          return;
        }
        const replacement = await this.#request(
          () => this.#syncApi.issueMultipartPartUrl({
            sessionId: plan.sessionId,
            partNumber
          })
        );
        this.#validatePartRange(replacement, partNumber, fileBytes.byteLength);
        const secondPut = await this.#request(
          () => this.#syncApi.putMultipartPartBytes({
            url: replacement.url,
            contentBytes: fileBytes.subarray(
              replacement.offsetBytes,
              replacement.offsetBytes + replacement.sizeBytes
            )
          })
        );
        if (secondPut === "url_rejected") {
          safeReasonRef.current = "multipart_part_url_rejected";
          await this.#persistProgress(
            event,
            plan,
            completedPartNumbers,
            status.state,
            "multipart_part_url_rejected"
          );
          throw new SyncApiError("server_error");
        }
        await this.#recordPartCompletion(
          event,
          plan,
          completedPartNumbers,
          partNumber,
          safeReasonRef.current
        );
      } finally {
        semaphore.release();
      }
    } catch (error) {
      if (stopState.firstFailure === null) {
        stopState.firstFailure = error;
      }
    }
  }
  // --- verdicts and progress ----------------------------------------------------------------------
  /**
   * The closed verdict of one observed session state: active states
   * return; a pending completion claim is the retryable
   * `multipart_completion_in_progress` replay; integrity and policy
   * verdicts map onto their terminal kinds; and a session that can never
   * accept work again clears its durable progress so the queue's retry
   * re-preflights the frozen event (spec 4.2, 8).
   */
  async #applySessionVerdict(eventId, state) {
    switch (state) {
      case "created":
      case "uploading":
        return;
      case "completing":
      case "verifying":
      case "promoting":
        throw new SyncApiError("operation_retry_required");
      case "integrity_failed":
        throw new SyncApiError("integrity_failed");
      case "policy_denied":
        throw new SyncApiError("policy_denied");
      case "expired":
      case "cancelling":
      case "cleanup_pending":
      case "cleaned":
        await this.#repository.clearMultipartProgress(eventId);
        throw new SyncApiError("operation_retry_required");
      case "committed":
        throw new SyncApiError("server_error");
    }
  }
  /**
   * Stop the changed session (spec 4.3, 8): keep the already observed
   * local progress under the closed `multipart_local_content_changed`
   * token, request the exact abort when online — best effort, because an
   * offline abort never blocks the closed verdict and the server's expiry
   * cleanup owns the orphaned staging resources — and report the change
   * so the queue terminalizes the OLD event while the newer watcher event
   * uploads separately.
   */
  async #stopForLocalContentChange(event, plan, completedPartNumbers) {
    await this.#persistProgress(
      event,
      plan,
      completedPartNumbers,
      "uploading",
      "multipart_local_content_changed"
    );
    this.#recordMultipartFailure("multipart_verify", ["multipart_local_content_changed"]);
    try {
      await this.#request(() => this.#syncApi.abortMultipartUploadSession(plan.sessionId));
    } catch (error) {
      this.#recordMultipartFailure("multipart_cleanup", multipartFailureTokensOf(error));
    }
    return { outcome: "local_content_changed" };
  }
  /** Record one landed part into the durable safe progress, strictly ascending. */
  async #recordPartCompletion(event, plan, completedPartNumbers, partNumber, safeReason) {
    completedPartNumbers.add(partNumber);
    await this.#persistProgress(event, plan, completedPartNumbers, "uploading", safeReason);
  }
  /** Persist one safe progress snapshot; the set sorts ascending before SQL. */
  async #persistProgress(event, plan, completedPartNumbers, sessionState, safeReason) {
    await this.#repository.saveMultipartProgress({
      eventId: event.eventId,
      sessionId: plan.sessionId,
      partSizeBytes: plan.partSizeBytes,
      partCount: plan.partCount,
      expiresAtEpochMs: plan.expiresAtEpochMs,
      completedPartNumbers: [...completedPartNumbers].sort((left, right) => left - right),
      sessionState,
      safeReason
    });
  }
  // --- frozen file and range guards ------------------------------------------------------------------
  /**
   * Open the current Vault bytes and compare them to the frozen journal
   * fingerprint: the exact byte size first, then the exact SHA-256. A
   * vanished file reports `missing`; a mismatched generation reports
   * `changed` and never lets its bytes enter the session.
   */
  async #checkFrozenFile(event, localFile) {
    const intent = this.#repository.readPendingRenameIntentForLocalFile(event.localFileId);
    const bytes = await this.#fileBytesReader.readRegularFileBytes(
      intent?.currentPath ?? localFile.normalizedPath
    );
    if (bytes === null) {
      return { kind: "missing" };
    }
    if (bytes.byteLength !== event.fingerprint.sizeBytes) {
      return { kind: "changed" };
    }
    const fingerprint = await deriveFrozenFingerprint(bytes);
    if (fingerprint.sha256 !== event.fingerprint.sha256) {
      return { kind: "changed" };
    }
    return { kind: "bytes", bytes };
  }
  /**
   * The server derives each range from the frozen geometry (spec 5): an
   * authorization whose part number, offset or window disagrees with the
   * local frozen bytes is malformed and never transmitted.
   */
  #validatePartRange(authorization, partNumber, fileSizeBytes) {
    const expectedOffsetBytes = (partNumber - 1) * MULTIPART_PART_SIZE_BYTES;
    const expectedSizeBytes = Math.min(
      MULTIPART_PART_SIZE_BYTES,
      fileSizeBytes - expectedOffsetBytes
    );
    if (authorization.partNumber !== partNumber || authorization.offsetBytes !== expectedOffsetBytes || authorization.sizeBytes !== expectedSizeBytes || authorization.offsetBytes + authorization.sizeBytes > fileSizeBytes) {
      throw new SyncApiError("server_error");
    }
  }
  // --- small seams -------------------------------------------------------------------------------------
  /** The create call binds the same frozen operation the preflight decided (spec 5). */
  #createSessionInput(event, localFile) {
    const operation = localFile.sourceId === null ? "create" : "update";
    return {
      eventId: event.eventId,
      idempotencyKey: event.idempotencyKey,
      operation,
      localFileId: event.localFileId,
      sourceId: operation === "update" ? localFile.sourceId : null,
      baseVersionId: operation === "update" ? localFile.baseVersionId : null,
      normalizedLocator: localFile.normalizedPath,
      fingerprint: event.fingerprint,
      policyRevisionNumber: localFile.policyRevisionNumber
    };
  }
  /** One transport request under the per-request timeout. */
  #request(issue) {
    return raceRequestTimeout(issue(), this.#requestTimeoutMs);
  }
  /**
   * Append one closed `multipart_failure` trail entry: the phase's closed
   * stage token plus the failure's closed reason tokens. Fire-and-forget
   * like every trail seam — diagnostics never block the transfer — and a
   * null trail (self-contained unit runs) records nothing.
   */
  #recordMultipartFailure(stage, tokens) {
    void this.#diagnosticTrail?.append({ kind: "multipart_failure", tokens: [stage, ...tokens] });
  }
  #throwIfSuspended(signal) {
    if (signal?.aborted === true) {
      throw new SyncApiError("network_timeout");
    }
  }
  #isPastDeadline(passDeadlineEpochMs) {
    return passDeadlineEpochMs !== void 0 && this.#nowEpochMs() >= passDeadlineEpochMs;
  }
  #isRunStopped(stopState, context) {
    return stopState.hasContentChanged || stopState.firstFailure !== null || stopState.deadlineReached || context.signal?.aborted === true;
  }
};
function terminalOutcomeOf(result) {
  const receipt = {
    sourceId: result.sourceId,
    sourceVersionId: result.sourceVersionId,
    contentVersion: result.contentVersion
  };
  if (result.resultKind === "no_change") {
    return { outcome: "no_change", receipt };
  }
  return { outcome: "committed", receipt };
}

// src/device-sync/atomic-vault-mutation.ts
var TEMP_SIBLING_SUFFIX = "device-sync-tmp";
var ROLLBACK_SIBLING_SUFFIX = "device-sync-rb";
function parentPrefixOf(locator) {
  const lastSlash = locator.lastIndexOf("/");
  return lastSlash === -1 ? "" : `${locator.slice(0, lastSlash)}/`;
}
function baseNameOf(locator) {
  const lastSlash = locator.lastIndexOf("/");
  return lastSlash === -1 ? locator : locator.slice(lastSlash + 1);
}
function buildTempSiblingLocator(targetLocator, token) {
  return `${parentPrefixOf(targetLocator)}.${baseNameOf(targetLocator)}.${TEMP_SIBLING_SUFFIX}-${token}`;
}
function buildRollbackSiblingLocator(targetLocator, token) {
  return `${parentPrefixOf(targetLocator)}.${baseNameOf(targetLocator)}.${ROLLBACK_SIBLING_SUFFIX}-${token}`;
}
var AtomicVaultMutationFailure = class extends Error {
  stage;
  restoredToBase;
  constructor(stage, restoredToBase) {
    super(`atomic vault mutation failed: ${stage}`);
    this.name = "AtomicVaultMutationFailure";
    this.stage = stage;
    this.restoredToBase = restoredToBase;
  }
};
async function hashesTo(bytes, fingerprint) {
  if (bytes === null || fingerprint === null) {
    return false;
  }
  return bytes.byteLength === fingerprint.sizeBytes && await sha256Hex(bytes) === fingerprint.sha256;
}
async function trashQuietly(seam, locator) {
  try {
    await seam.trashLocator(locator);
  } catch {
  }
}
async function restoreVerifiedOldBytes(seam, targetLocator, rollbackLocator, expectedBaseFingerprint) {
  try {
    await seam.trashLocator(targetLocator);
    await seam.renameLocator(rollbackLocator, targetLocator);
  } catch {
    return false;
  }
  let restoredBytes;
  try {
    restoredBytes = await seam.readBytes(targetLocator);
  } catch {
    return false;
  }
  if (expectedBaseFingerprint === null) {
    return restoredBytes !== null;
  }
  return hashesTo(restoredBytes, expectedBaseFingerprint);
}
async function stageVerifyAndReplaceVaultContent(input) {
  const { seam, targetLocator, tempToken } = input;
  const tempLocator = buildTempSiblingLocator(targetLocator, tempToken);
  try {
    if (await seam.locatorExists(tempLocator)) {
      await seam.trashLocator(tempLocator);
    }
    await seam.createFile(tempLocator, input.bytes);
  } catch {
    throw new AtomicVaultMutationFailure("stage", false);
  }
  let stagedBytes;
  try {
    stagedBytes = await seam.readBytes(tempLocator);
  } catch {
    throw new AtomicVaultMutationFailure("verify_staged", false);
  }
  if (!await hashesTo(stagedBytes, input.expectedFinalFingerprint)) {
    await trashQuietly(seam, tempLocator);
    throw new AtomicVaultMutationFailure("verify_staged", false);
  }
  let currentBytes;
  try {
    currentBytes = await seam.readBytes(targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("prove_base", false);
  }
  const hasTarget = currentBytes !== null;
  if (hasTarget && input.expectedBaseFingerprint !== null && !await hashesTo(currentBytes, input.expectedBaseFingerprint)) {
    await trashQuietly(seam, tempLocator);
    throw new AtomicVaultMutationFailure("prove_base", false);
  }
  const rollbackLocator = hasTarget ? buildRollbackSiblingLocator(targetLocator, tempToken) : null;
  try {
    if (rollbackLocator !== null) {
      await seam.renameLocator(targetLocator, rollbackLocator);
    }
    await seam.renameLocator(tempLocator, targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("replace", false);
  }
  let finalBytes;
  try {
    finalBytes = await seam.readBytes(targetLocator);
  } catch {
    throw new AtomicVaultMutationFailure("verify_final", false);
  }
  if (!await hashesTo(finalBytes, input.expectedFinalFingerprint)) {
    const restoredToBase = rollbackLocator !== null ? await restoreVerifiedOldBytes(
      seam,
      targetLocator,
      rollbackLocator,
      input.expectedBaseFingerprint
    ) : false;
    throw new AtomicVaultMutationFailure("verify_final", restoredToBase);
  }
  return { rollbackLocator, restoredToBase: false };
}
async function cleanupExactVaultSiblings(seam, input) {
  const ownedSiblings = [
    buildTempSiblingLocator(input.targetLocator, input.tempToken),
    buildRollbackSiblingLocator(input.targetLocator, input.tempToken)
  ];
  let hasCleanupFailure = false;
  for (const sibling of ownedSiblings) {
    try {
      if (await seam.locatorExists(sibling)) {
        await seam.trashLocator(sibling);
      }
    } catch {
      hasCleanupFailure = true;
    }
  }
  return !hasCleanupFailure;
}

// src/conflicts/api.ts
var ConflictApiError = class extends Error {
  kind;
  canRetry;
  requestId;
  wireErrorCode;
  isCredentialAbsent;
  constructor(kind, canRetry, requestId = null, wireErrorCode = null, isCredentialAbsent = false) {
    super(`conflict api failed: ${kind}`);
    this.name = "ConflictApiError";
    this.kind = kind;
    this.canRetry = canRetry;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
    this.isCredentialAbsent = isCredentialAbsent;
  }
};
function conflictApiError(kind, canRetry, requestId = null, wireErrorCode = null, isCredentialAbsent = false) {
  return new ConflictApiError(kind, canRetry, requestId, wireErrorCode, isCredentialAbsent);
}
var UUID_PATTERN7 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var CANONICAL_MEDIA_TYPE_PATTERN = /^[a-z0-9]+\/[a-z0-9.-]+$/;
var NON_NEGATIVE_INTEGER_TEXT_PATTERN = /^(0|[1-9][0-9]*)$/;
function parseEnvelopeRequestId2(value) {
  return typeof value === "string" && UUID_PATTERN7.test(value) ? value : null;
}
function isRecord3(value) {
  return typeof value === "object" && value !== null;
}
function parseEnvelope3(status, bodyText) {
  let parsed;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure2(status, null, null);
  }
  if (!isRecord3(parsed)) {
    throw mapWireFailure2(status, null, null);
  }
  const envelope = parsed;
  const requestId = parseEnvelopeRequestId2(envelope.request_id);
  if (envelope.error !== null && envelope.error !== void 0) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : null;
    throw mapWireFailure2(status, code, requestId);
  }
  if (envelope.data === null || envelope.data === void 0) {
    throw mapWireFailure2(status, null, requestId);
  }
  return { data: envelope.data, requestId };
}
function mapWireFailure2(status, code, requestId) {
  switch (code) {
    case "source_conflict_not_found":
      return conflictApiError("conflict_not_found", false, requestId, code);
    case "source_conflict_state_invalid":
      return conflictApiError("conflict_state_invalid", false, requestId, code);
    case "source_conflict_idempotency_mismatch":
      return conflictApiError("conflict_idempotency_mismatch", false, requestId, code);
    case "source_conflict_evidence_unavailable":
      return conflictApiError("evidence_unavailable", false, requestId, code);
    case "source_conflict_evidence_integrity_failed":
      return conflictApiError("evidence_integrity_failed", false, requestId, code);
    case "source_conflict_input_invalid":
      return conflictApiError("input_invalid", false, requestId, code);
    case "source_conflict_dependency_unavailable":
      return conflictApiError("dependency_unavailable", true, requestId, code);
    case "source_conflict_commit_outcome_unknown":
      return conflictApiError("commit_outcome_unknown", true, requestId, code);
    case "exclusion_policy_denied":
      return conflictApiError("policy_denied", false, requestId, code);
    default:
      break;
  }
  if (status === 401) {
    return conflictApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    return code === null ? conflictApiError("server_error", true, requestId, code) : conflictApiError("login_required", false, requestId, code);
  }
  if (status === 429) {
    return conflictApiError("network_rate_limited", true, requestId, code);
  }
  return conflictApiError("server_error", true, requestId, code);
}
function createConflictApi(options) {
  const { transport, resolveOrigin, getAccessToken } = options;
  function requireAccessToken() {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      throw conflictApiError("login_required", false, null, null, true);
    }
    return accessToken;
  }
  function requireUuidPath(value) {
    if (typeof value !== "string" || !UUID_PATTERN7.test(value)) {
      throw conflictApiError("input_invalid", false, null, null);
    }
    return encodeURIComponent(value);
  }
  async function send(request) {
    try {
      return await transport(request);
    } catch (error) {
      const name = error instanceof Error ? error.name : "";
      if (name === "TimeoutError" || name === "AbortError") {
        throw conflictApiError("network_timeout", true);
      }
      throw conflictApiError("network_offline", true);
    }
  }
  async function performJson(accessToken, request) {
    const response = await send({
      ...request,
      headers: { ...request.headers, authorization: `Bearer ${accessToken}` }
    });
    return parseEnvelope3(response.status, response.bodyText);
  }
  return {
    async listConflicts(input = {}) {
      const accessToken = requireAccessToken();
      const query = new URLSearchParams();
      if (input.limit !== void 0) {
        if (typeof input.limit !== "number" || !Number.isInteger(input.limit) || input.limit < 1 || input.limit > 200) {
          throw conflictApiError("input_invalid", false);
        }
        query.set("limit", String(input.limit));
      }
      if (input.exclusiveStartConflictId !== void 0 && input.exclusiveStartConflictId !== null) {
        if (!UUID_PATTERN7.test(input.exclusiveStartConflictId)) {
          throw conflictApiError("input_invalid", false);
        }
        query.set("exclusive_start_conflict_id", input.exclusiveStartConflictId);
      }
      const suffix = query.size > 0 ? `?${query.toString()}` : "";
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts${suffix}`,
        method: "GET",
        headers: { accept: "application/json" }
      });
      return decodeConflictPage(data);
    },
    async getConflict(conflictId) {
      const accessToken = requireAccessToken();
      const conflictPath = requireUuidPath(conflictId);
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}`,
        method: "GET",
        headers: { accept: "application/json" }
      });
      return decodeConflictDetail(data);
    },
    async downloadConflictEvidence(input) {
      const accessToken = requireAccessToken();
      if (!isConflictEvidenceRole(input.role)) {
        throw conflictApiError("input_invalid", false);
      }
      const conflictPath = requireUuidPath(input.conflictId);
      const response = await send({
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}/evidence/${input.role}`,
        method: "GET",
        headers: { authorization: `Bearer ${accessToken}`, accept: "application/octet-stream" }
      });
      if (response.status !== 200) {
        parseEnvelope3(response.status, response.bodyText);
        throw conflictApiError("server_error", true);
      }
      const headers = response.headers;
      const mediaType = headers["content-type"];
      const declaredSizeText = headers["content-length"];
      const bodyBytes = response.bodyBytes;
      const declaredSize = typeof declaredSizeText === "string" && NON_NEGATIVE_INTEGER_TEXT_PATTERN.test(declaredSizeText) ? Number.parseInt(declaredSizeText, 10) : null;
      if (bodyBytes === null || declaredSize === null || declaredSize > Number.MAX_SAFE_INTEGER || typeof mediaType !== "string" || !CANONICAL_MEDIA_TYPE_PATTERN.test(mediaType)) {
        throw conflictApiError("evidence_download_invalid", false);
      }
      const bytes = new Uint8Array(bodyBytes);
      if (bytes.byteLength !== declaredSize) {
        throw conflictApiError("evidence_download_invalid", false);
      }
      return { bytes, mediaType, sizeBytes: declaredSize };
    },
    async uploadResolutionCandidate(input) {
      const accessToken = requireAccessToken();
      const conflictPath = requireUuidPath(input.conflictId);
      if (typeof input.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(input.sha256) || typeof input.mediaType !== "string" || !CANONICAL_MEDIA_TYPE_PATTERN.test(input.mediaType) || !(input.bytes instanceof Uint8Array)) {
        throw conflictApiError("input_invalid", false);
      }
      const body = input.bytes.buffer.slice(
        input.bytes.byteOffset,
        input.bytes.byteOffset + input.bytes.byteLength
      );
      const response = await send({
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}/candidate`,
        method: "PUT",
        headers: {
          authorization: `Bearer ${accessToken}`,
          "content-type": "application/octet-stream",
          accept: "application/json",
          "x-candidate-sha256": input.sha256,
          "x-candidate-media-type": input.mediaType
        },
        body
      });
      if (response.status !== 200) {
        parseEnvelope3(response.status, response.bodyText);
        throw conflictApiError("server_error", true);
      }
      const { data } = parseEnvelope3(response.status, response.bodyText);
      if (!isRecord3(data) || typeof data["verified_candidate_object_id"] !== "string") {
        throw conflictApiError("server_error", true);
      }
      const verifiedCandidateObjectId = data["verified_candidate_object_id"];
      if (!UUID_PATTERN7.test(verifiedCandidateObjectId)) {
        throw conflictApiError("server_error", true);
      }
      return verifiedCandidateObjectId;
    },
    async resolveConflict(input) {
      const accessToken = requireAccessToken();
      try {
        validateConflictResolveInput(input);
      } catch {
        throw conflictApiError("input_invalid", false);
      }
      const reviewedRemoteVersionId = input.reviewedRemoteVersionId ?? null;
      const verifiedCandidateObjectId = input.verifiedCandidateObjectId ?? null;
      const wireBody = JSON.stringify({
        resolution_event_id: input.resolutionEventId,
        idempotency_key: input.idempotencyKey,
        resolution_kind: input.resolutionKind,
        ...reviewedRemoteVersionId === null ? {} : { reviewed_remote_version_id: reviewedRemoteVersionId },
        ...verifiedCandidateObjectId === null ? {} : { verified_candidate_object_id: verifiedCandidateObjectId }
      });
      const conflictPath = requireUuidPath(input.conflictId);
      const { data } = await performJson(accessToken, {
        url: `${resolveOrigin()}/api/sync/conflicts/${conflictPath}/resolve`,
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json"
        },
        body: wireBody
      });
      return decodeConflictResolution(data);
    }
  };
}

// src/conflicts/merge.ts
var MERGE_INPUT_MAXIMUM_BYTES = 262144;
var MERGE_INPUT_MAXIMUM_LINES = 4096;
var MERGE_PROPOSAL_MAXIMUM_BYTES = 524288;
var MERGE_SUPPORTED_MEDIA_TYPES = ["text/markdown", "text/plain"];
function isMergeSupportedMediaType(mediaType) {
  return MERGE_SUPPORTED_MEDIA_TYPES.includes(mediaType);
}
var MERGE_CONFLICT_LOCAL_OPEN_MARKER = "<<<<<<< local";
var MERGE_CONFLICT_SEPARATOR_MARKER = "=======";
var MERGE_CONFLICT_REMOTE_CLOSE_MARKER = ">>>>>>> remote";
var FATAL_UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });
function decodeConflictEvidenceText(bytes, mediaType) {
  if (!isMergeSupportedMediaType(mediaType)) {
    return { kind: "media_unsupported" };
  }
  if (bytes.byteLength > MERGE_INPUT_MAXIMUM_BYTES) {
    return { kind: "bytes_exceeded" };
  }
  try {
    return { kind: "text", text: FATAL_UTF8_DECODER.decode(bytes) };
  } catch {
    return { kind: "text_undecodable" };
  }
}
var TEXT_ENCODER = new TextEncoder();
function splitLines(text) {
  return text.split("\n");
}
function utf8ByteLength2(text) {
  return TEXT_ENCODER.encode(text).byteLength;
}
function longestMatchingBlock(baseLines, baseFrom, baseTo, otherLines, otherFrom, otherTo, otherIndex) {
  let bestBaseStart = baseFrom;
  let bestOtherStart = otherFrom;
  let bestLength = 0;
  let previousChain = /* @__PURE__ */ new Map();
  for (let baseIndex = baseFrom; baseIndex < baseTo; baseIndex += 1) {
    const chain = /* @__PURE__ */ new Map();
    const baseLine = baseLines[baseIndex];
    if (baseLine !== void 0) {
      const otherPositions = otherIndex.get(baseLine);
      if (otherPositions !== void 0) {
        for (const otherPosition of otherPositions) {
          if (otherPosition < otherFrom) {
            continue;
          }
          if (otherPosition >= otherTo) {
            break;
          }
          const runLength = (previousChain.get(otherPosition - 1) ?? 0) + 1;
          chain.set(otherPosition, runLength);
          if (runLength > bestLength) {
            bestLength = runLength;
            bestBaseStart = baseIndex - runLength + 1;
            bestOtherStart = otherPosition - runLength + 1;
          }
        }
      }
    }
    previousChain = chain;
  }
  return { baseStart: bestBaseStart, otherStart: bestOtherStart, length: bestLength };
}
function computeSideRegions(baseLines, otherLines) {
  const otherIndex = /* @__PURE__ */ new Map();
  for (let index = 0; index < otherLines.length; index += 1) {
    const line = otherLines[index];
    if (line === void 0) {
      continue;
    }
    const positions = otherIndex.get(line);
    if (positions === void 0) {
      otherIndex.set(line, [index]);
    } else {
      positions.push(index);
    }
  }
  const queue = [{ baseFrom: 0, baseTo: baseLines.length, otherFrom: 0, otherTo: otherLines.length }];
  const stableBlocks = [];
  while (queue.length > 0) {
    const window2 = queue.pop();
    if (window2 === void 0) {
      break;
    }
    const block = longestMatchingBlock(
      baseLines,
      window2.baseFrom,
      window2.baseTo,
      otherLines,
      window2.otherFrom,
      window2.otherTo,
      otherIndex
    );
    if (block.length === 0) {
      continue;
    }
    stableBlocks.push(block);
    if (window2.baseFrom < block.baseStart || window2.otherFrom < block.otherStart) {
      queue.push({
        baseFrom: window2.baseFrom,
        baseTo: block.baseStart,
        otherFrom: window2.otherFrom,
        otherTo: block.otherStart
      });
    }
    if (block.baseStart + block.length < window2.baseTo || block.otherStart + block.length < window2.otherTo) {
      queue.push({
        baseFrom: block.baseStart + block.length,
        baseTo: window2.baseTo,
        otherFrom: block.otherStart + block.length,
        otherTo: window2.otherTo
      });
    }
  }
  stableBlocks.sort((left, right) => left.baseStart - right.baseStart);
  const regions = [];
  let baseCursor = 0;
  let otherCursor = 0;
  for (const block of stableBlocks) {
    if (block.baseStart > baseCursor || block.otherStart > otherCursor) {
      regions.push({
        isStable: false,
        baseStart: baseCursor,
        baseEnd: block.baseStart,
        otherStart: otherCursor,
        otherEnd: block.otherStart
      });
    }
    regions.push({
      isStable: true,
      baseStart: block.baseStart,
      baseEnd: block.baseStart + block.length,
      otherStart: block.otherStart,
      otherEnd: block.otherStart + block.length
    });
    baseCursor = block.baseStart + block.length;
    otherCursor = block.otherStart + block.length;
  }
  if (baseCursor < baseLines.length || otherCursor < otherLines.length) {
    regions.push({
      isStable: false,
      baseStart: baseCursor,
      baseEnd: baseLines.length,
      otherStart: otherCursor,
      otherEnd: otherLines.length
    });
  }
  return regions;
}
function unstableHunksOf(regions) {
  return regions.filter((region) => !region.isStable).map((region) => ({
    baseStart: region.baseStart,
    baseEnd: region.baseEnd,
    otherStart: region.otherStart,
    otherEnd: region.otherEnd
  }));
}
function sideRegionLines(baseLines, sideLines, sideHunks, regionStart, regionEnd) {
  const lines = [];
  let baseCursor = regionStart;
  for (const hunk of sideHunks) {
    if (hunk.baseStart > baseCursor) {
      lines.push(...baseLines.slice(baseCursor, hunk.baseStart));
    }
    lines.push(...sideLines.slice(hunk.otherStart, hunk.otherEnd));
    baseCursor = hunk.baseEnd;
  }
  if (baseCursor < regionEnd) {
    lines.push(...baseLines.slice(baseCursor, regionEnd));
  }
  return lines;
}
function computeBoundedThreeWayMerge(base, remote, local) {
  const inputs = [base, remote, local];
  for (const input of inputs) {
    if (utf8ByteLength2(input) > MERGE_INPUT_MAXIMUM_BYTES) {
      return boundExceeded();
    }
    if (splitLines(input).length > MERGE_INPUT_MAXIMUM_LINES) {
      return boundExceeded();
    }
  }
  const baseLines = splitLines(base);
  const remoteLines = splitLines(remote);
  const localLines = splitLines(local);
  const remoteHunks = unstableHunksOf(computeSideRegions(baseLines, remoteLines));
  const localHunks = unstableHunksOf(computeSideRegions(baseLines, localLines));
  const tagged = [
    ...remoteHunks.map((hunk) => ({ ...hunk, side: "remote" })),
    ...localHunks.map((hunk) => ({ ...hunk, side: "local" }))
  ];
  tagged.sort(
    (left, right) => left.baseStart - right.baseStart || (left.side === right.side ? 0 : left.side === "remote" ? -1 : 1)
  );
  const mergedLines = [];
  let conflictingHunkCount = 0;
  let baseCursor = 0;
  let hunkIndex = 0;
  while (hunkIndex < tagged.length) {
    const lead = tagged[hunkIndex];
    if (lead === void 0) {
      break;
    }
    if (lead.baseStart > baseCursor) {
      mergedLines.push(...baseLines.slice(baseCursor, lead.baseStart));
      baseCursor = lead.baseStart;
    }
    const regionStart = baseCursor;
    let regionEnd = lead.baseEnd;
    const regionHunks = [lead];
    hunkIndex += 1;
    for (; ; ) {
      const follower = tagged[hunkIndex];
      if (follower === void 0 || follower.baseStart > regionEnd) {
        break;
      }
      regionEnd = Math.max(regionEnd, follower.baseEnd);
      regionHunks.push(follower);
      hunkIndex += 1;
    }
    baseCursor = regionEnd;
    const regionRemoteHunks = regionHunks.filter((hunk) => hunk.side === "remote");
    const regionLocalHunks = regionHunks.filter((hunk) => hunk.side === "local");
    if (regionRemoteHunks.length === 0) {
      mergedLines.push(
        ...sideRegionLines(baseLines, localLines, regionLocalHunks, regionStart, regionEnd)
      );
      continue;
    }
    if (regionLocalHunks.length === 0) {
      mergedLines.push(
        ...sideRegionLines(baseLines, remoteLines, regionRemoteHunks, regionStart, regionEnd)
      );
      continue;
    }
    const remoteRegionLines = sideRegionLines(
      baseLines,
      remoteLines,
      regionRemoteHunks,
      regionStart,
      regionEnd
    );
    const localRegionLines = sideRegionLines(
      baseLines,
      localLines,
      regionLocalHunks,
      regionStart,
      regionEnd
    );
    if (linesEqual(remoteRegionLines, localRegionLines)) {
      mergedLines.push(...remoteRegionLines);
    } else {
      conflictingHunkCount += 1;
      mergedLines.push(
        MERGE_CONFLICT_LOCAL_OPEN_MARKER,
        ...localRegionLines,
        MERGE_CONFLICT_SEPARATOR_MARKER,
        ...remoteRegionLines,
        MERGE_CONFLICT_REMOTE_CLOSE_MARKER
      );
    }
  }
  if (baseCursor < baseLines.length) {
    mergedLines.push(...baseLines.slice(baseCursor));
  }
  const mergedText = mergedLines.join("\n");
  if (utf8ByteLength2(mergedText) > MERGE_PROPOSAL_MAXIMUM_BYTES) {
    return boundExceeded();
  }
  return {
    outcome: conflictingHunkCount > 0 ? "merged_with_conflicts" : "merged_clean",
    requiresUserReview: conflictingHunkCount > 0,
    mergedText,
    conflictingHunkCount
  };
}
function boundExceeded() {
  return {
    outcome: "bound_exceeded",
    requiresUserReview: true,
    mergedText: null,
    conflictingHunkCount: 0
  };
}
function linesEqual(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      return false;
    }
  }
  return true;
}

// src/conflicts/controller.ts
var CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS = [
  "conflict_choice_unavailable",
  "conflict_evidence_unavailable",
  "conflict_media_unsupported",
  "conflict_text_undecodable",
  "conflict_merge_bound_exceeded",
  "conflict_candidate_upload_failed",
  "conflict_winner_download_failed",
  "conflict_vault_apply_failed",
  "conflict_apply_retry_exhausted"
];
var ConflictControllerError = class extends Error {
  reason;
  constructor(reason) {
    super(`conflict controller failed: ${reason}`);
    this.name = "ConflictControllerError";
    this.reason = reason;
  }
};
var CONFLICT_MERGE_UPLOAD_MEDIA_TYPE = "text/markdown";
var CanonicalApplyError = class extends Error {
  stage;
  constructor(stage) {
    super(`canonical apply failed: ${stage}`);
    this.name = "CanonicalApplyError";
    this.stage = stage;
  }
};
function createUuidConflictResolutionIdentityMinter() {
  return () => ({
    resolutionEventId: crypto.randomUUID(),
    idempotencyKey: crypto.randomUUID()
  });
}
var CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS = 5;
var CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS = 1e3;
var CONFLICT_LOCAL_APPLY_RETRY_MAXIMUM_DELAY_MS = 6e4;
function conflictLocalApplyRetryDelayMs(failedAttemptCount) {
  if (failedAttemptCount < 1) {
    return CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS;
  }
  const exponential = CONFLICT_LOCAL_APPLY_RETRY_BASE_DELAY_MS * 2 ** (failedAttemptCount - 1);
  return Math.min(exponential, CONFLICT_LOCAL_APPLY_RETRY_MAXIMUM_DELAY_MS);
}
function isRemoteTombstoneKind(kind) {
  return kind === "edit_remote_delete" || kind === "delete_remote_edit";
}
function planWinnerOfResolution(detail, resolution, mergedBytes) {
  if (resolution.resolutionKind === "keep_remote") {
    if (isRemoteTombstoneKind(detail.conflictKind)) {
      return {
        targetAction: "apply_remote_tombstone",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null
      };
    }
    return {
      targetAction: "apply_remote_version",
      winnerVersionId: detail.observedRemoteVersionId,
      winnerBytes: null,
      winnerMediaType: null
    };
  }
  return {
    targetAction: "apply_resulting_version",
    winnerVersionId: resolution.resultingVersionId,
    winnerBytes: mergedBytes,
    winnerMediaType: mergedBytes === null ? null : CONFLICT_MERGE_UPLOAD_MEDIA_TYPE
  };
}
function planWinnerOfParkedRetry(detail, parked) {
  switch (parked.targetAction) {
    case "apply_remote_tombstone":
      return {
        targetAction: "apply_remote_tombstone",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null
      };
    case "apply_remote_version":
      return {
        targetAction: "apply_remote_version",
        winnerVersionId: detail.observedRemoteVersionId,
        winnerBytes: null,
        winnerMediaType: null
      };
    case "apply_resulting_version":
      return {
        targetAction: "apply_resulting_version",
        winnerVersionId: detail.resultingVersionId,
        winnerBytes: null,
        winnerMediaType: null
      };
  }
}
function safeReasonOfApplyStage(stage) {
  return stage === "winner_download" ? "winner_download_failed" : "vault_apply_failed";
}
function diagnosticOfApplyStage(stage) {
  return stage === "winner_download" ? "conflict_winner_download_failed" : "conflict_vault_apply_failed";
}
function applyStageOf(error) {
  return error instanceof CanonicalApplyError && error.stage === "winner_download" ? "winner_download" : "vault_apply";
}
function createConflictController(options) {
  const { api, repairStore, uploader, applier } = options;
  const mintIdentity = options.mintIdentity ?? createUuidConflictResolutionIdentityMinter();
  const clock = options.clock ?? Date.now;
  const diagnostics = options.diagnostics ?? null;
  const inFlightIdentities = /* @__PURE__ */ new Map();
  function observe(reason) {
    diagnostics?.observeConflictFailure(reason);
  }
  async function recordApplyFailure(conflictId, resolutionEventId, stage) {
    observe(diagnosticOfApplyStage(stage));
    const parked = repairStore.readPendingLocalApply(conflictId);
    const failedAttemptCount = (parked?.attemptCount ?? 0) + 1;
    const nowEpochMs = clock();
    if (failedAttemptCount >= CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS) {
      observe("conflict_apply_retry_exhausted");
    }
    await repairStore.recordLocalApplyFailure({
      conflictId,
      resolutionEventId,
      safeReason: safeReasonOfApplyStage(stage),
      nowEpochMs,
      nextEligibleRetryEpochMs: nowEpochMs + conflictLocalApplyRetryDelayMs(failedAttemptCount)
    });
  }
  async function applyParkedWinner(detail, resolution, plan) {
    try {
      await applier.applyCanonicalOutcome({
        conflictId: detail.conflictId,
        resolutionEventId: resolution.resolutionEventId,
        sourceId: detail.sourceId,
        targetAction: plan.targetAction,
        winnerVersionId: plan.winnerVersionId,
        winnerBytes: plan.winnerBytes,
        winnerMediaType: plan.winnerMediaType
      });
    } catch (error) {
      await recordApplyFailure(detail.conflictId, resolution.resolutionEventId, applyStageOf(error));
      return { kind: "local_apply_pending" };
    }
    await repairStore.completeLocalApply({
      conflictId: detail.conflictId,
      resolutionEventId: resolution.resolutionEventId
    });
    return { kind: "resolved_and_applied", resolution };
  }
  async function resolveChoice(conflictId, resolutionKind, editedText) {
    const detail = await api.getConflict(conflictId);
    if (!detail.choices.includes(resolutionKind)) {
      observe("conflict_choice_unavailable");
      throw new ConflictControllerError("conflict_choice_unavailable");
    }
    let verifiedCandidateObjectId = null;
    let mergedBytes = null;
    if (resolutionKind === "save_merged") {
      if (editedText === null) {
        throw new ConflictControllerError("conflict_choice_unavailable");
      }
      const encodedDraft = new TextEncoder().encode(editedText);
      if (encodedDraft.byteLength > MERGE_PROPOSAL_MAXIMUM_BYTES) {
        observe("conflict_merge_bound_exceeded");
        throw new ConflictControllerError("conflict_merge_bound_exceeded");
      }
      try {
        const receipt = await uploader.uploadVerifiedCandidate({
          conflictId,
          bytes: encodedDraft,
          mediaType: CONFLICT_MERGE_UPLOAD_MEDIA_TYPE
        });
        verifiedCandidateObjectId = receipt.verifiedCandidateObjectId;
        mergedBytes = encodedDraft;
      } catch {
        observe("conflict_candidate_upload_failed");
        throw new ConflictControllerError("conflict_candidate_upload_failed");
      }
    }
    const identity = inFlightIdentities.get(conflictId) ?? mintIdentity();
    inFlightIdentities.set(conflictId, identity);
    let resolution;
    try {
      resolution = await api.resolveConflict({
        conflictId,
        resolutionEventId: identity.resolutionEventId,
        idempotencyKey: identity.idempotencyKey,
        resolutionKind,
        reviewedRemoteVersionId: detail.observedRemoteVersionId,
        verifiedCandidateObjectId
      });
    } catch (error) {
      if (!(error instanceof ConflictApiError) || !error.canRetry) {
        inFlightIdentities.delete(conflictId);
      }
      throw error;
    }
    inFlightIdentities.delete(conflictId);
    if (resolution.outcome === "stale_successor") {
      return {
        kind: "stale_successor",
        successorConflictId: resolution.successorConflictId ?? conflictId
      };
    }
    const plan = planWinnerOfResolution(detail, resolution, mergedBytes);
    await repairStore.parkPendingLocalApply({
      conflictId,
      resolutionEventId: resolution.resolutionEventId,
      targetAction: plan.targetAction,
      safeReason: "resolution_committed",
      nowEpochMs: clock()
    });
    if (plan.winnerVersionId === null && plan.targetAction !== "apply_remote_tombstone") {
      await recordApplyFailure(conflictId, resolution.resolutionEventId, "winner_download");
      return { kind: "local_apply_pending" };
    }
    return applyParkedWinner(detail, resolution, plan);
  }
  return {
    async listOpenConflicts() {
      return api.listConflicts();
    },
    async getConflictDetail(conflictId) {
      return api.getConflict(conflictId);
    },
    async buildMergeProposal(conflictId) {
      const detail = await api.getConflict(conflictId);
      if (!detail.choices.includes("save_merged")) {
        return { kind: "manual_choice_required", reason: "merge_choice_not_admitted" };
      }
      const roles = ["base", "remote", "candidate"];
      const texts = /* @__PURE__ */ new Map();
      let candidateMediaType = CONFLICT_MERGE_UPLOAD_MEDIA_TYPE;
      for (const role of roles) {
        let evidence;
        try {
          evidence = await api.downloadConflictEvidence({ conflictId, role });
        } catch {
          observe("conflict_evidence_unavailable");
          return { kind: "manual_choice_required", reason: "evidence_role_unavailable" };
        }
        const decoded = decodeConflictEvidenceText(evidence.bytes, evidence.mediaType);
        if (decoded.kind !== "text") {
          if (decoded.kind === "bytes_exceeded") {
            observe("conflict_merge_bound_exceeded");
            return { kind: "manual_choice_required", reason: "merge_bound_exceeded" };
          }
          if (decoded.kind === "text_undecodable") {
            observe("conflict_text_undecodable");
            return { kind: "manual_choice_required", reason: "text_undecodable" };
          }
          observe("conflict_media_unsupported");
          return { kind: "manual_choice_required", reason: "media_unsupported" };
        }
        texts.set(role, decoded.text);
        if (role === "candidate") {
          candidateMediaType = evidence.mediaType;
        }
      }
      const baseText = texts.get("base") ?? "";
      const remoteText = texts.get("remote") ?? "";
      const localText = texts.get("candidate") ?? "";
      const merge = computeBoundedThreeWayMerge(baseText, remoteText, localText);
      if (merge.outcome === "bound_exceeded" || merge.mergedText === null) {
        observe("conflict_merge_bound_exceeded");
        return { kind: "manual_choice_required", reason: "merge_bound_exceeded" };
      }
      return {
        kind: "editable_merge",
        mergedText: merge.mergedText,
        requiresUserReview: merge.requiresUserReview,
        conflictingHunkCount: merge.conflictingHunkCount,
        mediaType: candidateMediaType
      };
    },
    async resolveKeepRemote(conflictId) {
      return resolveChoice(conflictId, "keep_remote", null);
    },
    async resolveKeepLocal(conflictId) {
      return resolveChoice(conflictId, "keep_local", null);
    },
    async resolveSaveMerged(conflictId, editedText) {
      return resolveChoice(conflictId, "save_merged", editedText);
    },
    async retryPendingLocalApplies() {
      const parkedRows = repairStore.readPendingLocalApplies();
      const nowEpochMs = clock();
      for (const parked of parkedRows) {
        if (parked.attemptCount >= CONFLICT_LOCAL_APPLY_MAXIMUM_ATTEMPTS || parked.nextEligibleRetryEpochMs === null || parked.nextEligibleRetryEpochMs > nowEpochMs) {
          continue;
        }
        let detail;
        try {
          detail = await api.getConflict(parked.conflictId);
        } catch {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, "winner_download");
          continue;
        }
        const plan = planWinnerOfParkedRetry(detail, parked);
        if (plan.winnerVersionId === null && plan.targetAction !== "apply_remote_tombstone") {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, "winner_download");
          continue;
        }
        try {
          await applier.applyCanonicalOutcome({
            conflictId: parked.conflictId,
            resolutionEventId: parked.resolutionEventId,
            sourceId: detail.sourceId,
            targetAction: plan.targetAction,
            winnerVersionId: plan.winnerVersionId,
            winnerBytes: null,
            winnerMediaType: null
          });
        } catch (error) {
          await recordApplyFailure(parked.conflictId, parked.resolutionEventId, applyStageOf(error));
          continue;
        }
        await repairStore.completeLocalApply({
          conflictId: parked.conflictId,
          resolutionEventId: parked.resolutionEventId
        });
      }
    }
  };
}

// src/conflicts/composition.ts
var CONFLICT_COMPOSITION_EXTRA_DIAGNOSTIC_REASONS = [
  "conflict_repair_store_failed",
  "conflict_apply_retry_failed",
  "conflict_echo_marker_failed"
];
var CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS = [
  ...CONFLICT_CONTROLLER_DIAGNOSTIC_REASONS,
  ...CONFLICT_COMPOSITION_EXTRA_DIAGNOSTIC_REASONS
];
function createConflictDiagnosticsTrailSink(trail) {
  return {
    observeConflictFailure(reason) {
      void trail.append({ kind: "conflict_failure", tokens: [reason] });
    },
    observeConflictCompositionFailure(reason, contextReason) {
      const tokens = contextReason === void 0 || contextReason === null ? [reason] : [reason, contextReason];
      void trail.append({ kind: "conflict_failure", tokens: [...tokens] });
    }
  };
}
var CLOSED_STORE_REASONS = new Set(JOURNAL_STORE_ERROR_REASONS);
function storeReasonOf(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string" && CLOSED_STORE_REASONS.has(reason)) {
      return reason;
    }
  }
  return null;
}
var UUID_PATTERN8 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function sqlText4(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
function readLocalFileLocatorBySourceId(database, sourceId) {
  if (sourceId === null || !UUID_PATTERN8.test(sourceId)) {
    return null;
  }
  const rows = database.readAll(
    `select normalized_path from local_files where source_id = ${sqlText4(sourceId)} order by rowid asc;`
  );
  const paths = [];
  for (const row of rows[0]?.values ?? []) {
    const [normalizedPath] = row;
    if (typeof normalizedPath === "string") {
      paths.push(normalizedPath);
    }
  }
  return paths.length === 1 ? paths[0] : null;
}
var CONFLICT_ECHO_MARKER_SEQUENCE_CEILING = Number.MAX_SAFE_INTEGER;
function createConflictEchoMarkerSequenceMinter() {
  let nextSequence = CONFLICT_ECHO_MARKER_SEQUENCE_CEILING;
  return () => {
    const sequence = nextSequence;
    nextSequence -= 1;
    return sequence;
  };
}
var ECHO_MARKER_SEQUENCE_ATTEMPTS = 3;
async function recordConflictEchoMarker(repository, minter, seed) {
  let lastError = null;
  for (let attempt = 0; attempt < ECHO_MARKER_SEQUENCE_ATTEMPTS; attempt += 1) {
    const marker = { eventSequence: minter(), ...seed };
    try {
      await repository.recordEchoMarker(marker);
      return marker;
    } catch (error) {
      lastError = error;
      if (storeReasonOf(error) !== "journal_mutation_failed") {
        throw error;
      }
    }
  }
  throw lastError;
}
async function trashQuietly2(seam, locator) {
  try {
    await seam.trashLocator(locator);
  } catch {
  }
}
async function readBytesOrFail(seam, locator) {
  try {
    return await seam.readBytes(locator);
  } catch {
    throw new CanonicalApplyError("vault_apply");
  }
}
function createConflictCanonicalOutcomeApplier(options) {
  const { database, repository, seam, downloadSourceVersion } = options;
  const diagnostics = options.diagnostics ?? null;
  const mintEchoMarkerSequence = options.mintEchoMarkerSequence ?? createConflictEchoMarkerSequenceMinter();
  function observeMarkerCleanupFailure(error) {
    diagnostics?.observeConflictCompositionFailure(
      "conflict_echo_marker_failed",
      storeReasonOf(error)
    );
  }
  async function consumeMarkerQuietly(marker) {
    const observation = {
      eventSequence: marker.eventSequence,
      sourceId: marker.sourceId,
      operation: marker.operation,
      priorLocator: marker.priorLocator,
      targetLocator: marker.targetLocator,
      fingerprint: marker.finalFingerprint
    };
    try {
      await repository.matchAndConsumeEcho(observation);
    } catch (error) {
      observeMarkerCleanupFailure(error);
    }
  }
  async function cleanFailedMutationSiblings(error, locator, tempToken) {
    if (!(error instanceof AtomicVaultMutationFailure)) {
      return;
    }
    switch (error.stage) {
      case "stage":
      case "verify_staged":
      case "prove_base":
        await cleanupExactVaultSiblings(seam, { targetLocator: locator, tempToken });
        return;
      case "replace":
        await trashQuietly2(seam, buildTempSiblingLocator(locator, tempToken));
        return;
      case "verify_final":
        return;
    }
  }
  async function resolveWinner(command) {
    if (command.winnerBytes !== null) {
      const bytes = command.winnerBytes;
      const fingerprint = command.winnerMediaType === null ? await deriveFrozenFingerprint(bytes) : {
        sha256: await sha256Hex(bytes),
        sizeBytes: bytes.byteLength,
        mediaType: command.winnerMediaType
      };
      return { bytes, fingerprint };
    }
    if (command.sourceId === null || command.winnerVersionId === null) {
      throw new CanonicalApplyError("winner_download");
    }
    let download;
    try {
      download = await downloadSourceVersion({
        sourceId: command.sourceId,
        sourceVersionId: command.winnerVersionId
      });
    } catch {
      throw new CanonicalApplyError("winner_download");
    }
    return {
      bytes: download.bytes,
      fingerprint: {
        sha256: download.declaredSha256,
        sizeBytes: download.sizeBytes,
        mediaType: download.mediaType
      }
    };
  }
  return {
    async applyCanonicalOutcome(command) {
      const locator = readLocalFileLocatorBySourceId(database, command.sourceId);
      if (locator === null) {
        throw new CanonicalApplyError("winner_download");
      }
      const sourceId = command.sourceId;
      if (command.targetAction === "apply_remote_tombstone") {
        const priorBytes = await readBytesOrFail(seam, locator);
        if (priorBytes === null) {
          return;
        }
        let marker2;
        try {
          marker2 = await recordConflictEchoMarker(repository, mintEchoMarkerSequence, {
            sourceId,
            operation: "deleted",
            priorLocator: locator,
            targetLocator: null,
            finalFingerprint: null
          });
        } catch {
          throw new CanonicalApplyError("vault_apply");
        }
        try {
          await seam.trashLocator(locator);
        } catch {
          await consumeMarkerQuietly(marker2);
          throw new CanonicalApplyError("vault_apply");
        }
        return;
      }
      const { bytes, fingerprint } = await resolveWinner(command);
      let marker;
      try {
        marker = await recordConflictEchoMarker(repository, mintEchoMarkerSequence, {
          sourceId,
          operation: "updated",
          priorLocator: locator,
          targetLocator: null,
          finalFingerprint: fingerprint
        });
      } catch {
        throw new CanonicalApplyError("vault_apply");
      }
      try {
        const mutated = await stageVerifyAndReplaceVaultContent({
          seam,
          targetLocator: locator,
          tempToken: command.resolutionEventId,
          bytes,
          expectedFinalFingerprint: fingerprint,
          // A conflict apply is canonical: it proves only the target's
          // SHAPE (occupied → retained rollback; absent → created
          // shape), never a pinned base — the resolution already decided
          // the winner over the local bytes.
          expectedBaseFingerprint: null
        });
        if (mutated.rollbackLocator !== null) {
          await trashQuietly2(seam, mutated.rollbackLocator);
        }
      } catch (error) {
        await cleanFailedMutationSiblings(error, locator, command.resolutionEventId);
        await consumeMarkerQuietly(marker);
        throw new CanonicalApplyError("vault_apply");
      }
    }
  };
}
function createConflictVerifiedCandidateUploader(api) {
  return {
    async uploadVerifiedCandidate(upload) {
      try {
        const digestHex = await sha256Hex(upload.bytes);
        const verifiedCandidateObjectId = await api.uploadResolutionCandidate({
          conflictId: upload.conflictId,
          bytes: upload.bytes,
          mediaType: upload.mediaType,
          sha256: digestHex
        });
        return { verifiedCandidateObjectId };
      } catch (error) {
        if (error instanceof ConflictControllerError) {
          throw error;
        }
        throw new ConflictControllerError("conflict_candidate_upload_failed");
      }
    }
  };
}
async function observeForeignThrow(sink, run) {
  try {
    return await run();
  } catch (error) {
    if (!(error instanceof ConflictApiError) && !(error instanceof ConflictControllerError)) {
      sink.observeConflictCompositionFailure("conflict_repair_store_failed", storeReasonOf(error));
    }
    throw error;
  }
}
function observeUnobservedConflictControllerFailures(controller, sink) {
  return {
    listOpenConflicts: () => observeForeignThrow(sink, () => controller.listOpenConflicts()),
    getConflictDetail: (conflictId) => observeForeignThrow(sink, () => controller.getConflictDetail(conflictId)),
    buildMergeProposal: (conflictId) => observeForeignThrow(sink, () => controller.buildMergeProposal(conflictId)),
    resolveKeepRemote: (conflictId) => observeForeignThrow(sink, () => controller.resolveKeepRemote(conflictId)),
    resolveKeepLocal: (conflictId) => observeForeignThrow(sink, () => controller.resolveKeepLocal(conflictId)),
    resolveSaveMerged: (conflictId, editedText) => observeForeignThrow(sink, () => controller.resolveSaveMerged(conflictId, editedText)),
    retryPendingLocalApplies: () => observeForeignThrow(sink, () => controller.retryPendingLocalApplies())
  };
}
function deriveConflictApplyStatusFacts(pending) {
  const tokens = [];
  for (const row of pending) {
    if (CONFLICT_LOCAL_REPAIR_SAFE_REASONS.includes(row.safeReason) && !tokens.includes(row.safeReason)) {
      tokens.push(row.safeReason);
    }
  }
  return { pendingLocalApplyCount: pending.length, localApplySafeReasonTokens: tokens };
}

// src/journal/sync-diagnostics-trail.ts
var SYNC_DIAGNOSTICS_TRAIL_FILE_NAME = "sync-diagnostics-trail.json";
var SYNC_DIAGNOSTICS_TRAIL_CONTRACT = "obsidian_sync_diagnostics_trail/v2";
var LEGACY_SYNC_DIAGNOSTICS_TRAIL_CONTRACTS = /* @__PURE__ */ new Set([
  "obsidian_sync_diagnostics_trail/v1"
]);
var MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES = 128;
var MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY = 8;
var MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES = 999;
var SYNC_DIAGNOSTIC_KINDS = [
  "wire_failure",
  "pass_outcome",
  "journal_failure",
  "publish_failure",
  "trail_reset",
  "self_check",
  "startup_failure",
  "credential_failure",
  "cursor_failure",
  "apply_failure",
  "reconcile_failure",
  "composition_read_failure",
  "multipart_failure",
  "conflict_failure"
];
var SYNC_EVENT_STATE_TOKENS = [
  "state_queued",
  "state_waiting_retry",
  "state_preflight",
  "state_uploading",
  "state_blocked_conflict",
  "state_excluded_policy",
  "state_blocked_size",
  "state_deferred_lifecycle",
  "state_integrity_failed",
  "state_committed",
  "state_no_change",
  "row_absent",
  "reason_unknown"
];
var SYNC_SELF_CHECK_VERDICT_TOKENS = [
  "trail_probe",
  "trail_persist_ok",
  "trail_persist_failed",
  "credential_present",
  "credential_absent",
  "origin_reachable",
  "origin_unreachable"
];
var SYNC_PARK_SITE_TOKENS = [
  "site_argument_validation",
  "site_mutation_internal"
];
var SYNC_STARTUP_STAGE_TOKENS = [
  "engine_load",
  "wasm_read",
  "journal_recovery",
  "other"
];
var SYNC_COMPOSITION_READ_FAILURE_TOKENS = [
  "status_read_failed",
  "note_status_read_failed",
  "retry_schedule_read_failed",
  "sync_status_read_failed",
  "queue_drain_failed",
  "snapshot_drain_failed",
  "settled_admission_failed",
  "automatic_snapshot_admission_failed",
  "lifecycle_reconcile_persist_failed",
  "restore_reservation_persist_failed",
  "pending_rename_intent_read_failed",
  "pending_rename_intent_persist_failed",
  "pending_rename_intent_conflict",
  "pending_rename_intent_exhausted",
  "pending_rename_intent_lifecycle_rejected"
];
var SYNC_MULTIPART_FAILURE_STAGE_TOKENS = [
  "multipart_resume",
  "multipart_verify",
  "multipart_cleanup"
];
function envelopeRequestId(requestId) {
  return UUID_PATTERN9.test(requestId) ? { requestId } : null;
}
function envelopeErrorCode(code) {
  return isSyncApiEnvelopeErrorCode(code) ? code : null;
}
var UUID_PATTERN9 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var QUEUE_PASS_OUTCOME_TOKENS = [
  "completed",
  "deadline_reached",
  "stopped",
  "login_required",
  "retry_scheduled",
  "pass_already_running",
  "pass_wrapper_failed",
  "pass_stall_recovered"
];
var LIFECYCLE_RUN_OUTCOME_TOKENS = [
  "idle",
  "committed",
  "blocked",
  "retry",
  "login_required"
];
var CLOSED_DIAGNOSTIC_TOKEN_SET = /* @__PURE__ */ new Set([
  ...QUEUE_PASS_OUTCOME_TOKENS,
  ...JOURNAL_SAFE_ERROR_LABELS,
  ...JOURNAL_STORE_ERROR_REASONS,
  ...SYNC_API_FAILURE_KINDS,
  ...LIFECYCLE_RUN_OUTCOME_TOKENS,
  ...SYNC_SELF_CHECK_VERDICT_TOKENS,
  ...SYNC_EVENT_STATE_TOKENS,
  ...SYNC_PARK_SITE_TOKENS,
  ...SYNC_STARTUP_STAGE_TOKENS,
  ...SYNC_COMPOSITION_READ_FAILURE_TOKENS,
  ...SYNC_API_ENVELOPE_ERROR_CODES,
  // The device-sync reason families and stage vocabularies (task 7).
  ...DEVICE_SYNC_SERVER_REASONS,
  ...DEVICE_SYNC_ACTION_REASONS,
  ...DEVICE_SYNC_TRANSPORT_REASONS,
  ...DEVICE_SYNC_LOCAL_REASONS,
  ...DEVICE_SYNC_CURSOR_STAGES,
  ...DEVICE_SYNC_APPLY_STAGES,
  ...DEVICE_SYNC_RECONCILE_STAGES,
  ...DEVICE_SYNC_CREDENTIAL_STAGES,
  ...DEVICE_SYNC_COMPOSITION_READ_STAGES,
  // The multipart failure stages and safe-reason tokens (multipart task 11).
  ...SYNC_MULTIPART_FAILURE_STAGE_TOKENS,
  ...MULTIPART_SAFE_REASON_TOKENS,
  // The conflict composition reasons (conflict inbox task 9).
  ...CONFLICT_COMPOSITION_DIAGNOSTIC_REASONS
]);
var SYNC_API_ENVELOPE_ERROR_CODE_SET = new Set(
  SYNC_API_ENVELOPE_ERROR_CODES
);
function isSyncDiagnosticClosedToken(value) {
  return typeof value === "string" && CLOSED_DIAGNOSTIC_TOKEN_SET.has(value);
}
function isSyncApiEnvelopeErrorCode(value) {
  return typeof value === "string" && SYNC_API_ENVELOPE_ERROR_CODE_SET.has(value);
}
var DIAGNOSTIC_KIND_SET = new Set(SYNC_DIAGNOSTIC_KINDS);
function isRecord4(value) {
  return typeof value === "object" && value !== null;
}
function parseTrailToken(value) {
  if (isSyncDiagnosticClosedToken(value)) {
    return value;
  }
  if (isRecord4(value)) {
    const requestId = value["request_id"];
    if (typeof requestId === "string") {
      return envelopeRequestId(requestId);
    }
  }
  return null;
}
function parseTrailSidecar(bytes) {
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  if (!isRecord4(parsed) || parsed["contract"] !== SYNC_DIAGNOSTICS_TRAIL_CONTRACT && !LEGACY_SYNC_DIAGNOSTICS_TRAIL_CONTRACTS.has(parsed["contract"])) {
    return null;
  }
  const rawEntries = parsed["entries"];
  if (!Array.isArray(rawEntries)) {
    return null;
  }
  const entries = [];
  for (const rawEntry of rawEntries) {
    if (!isRecord4(rawEntry)) {
      return null;
    }
    const kind = rawEntry["kind"];
    const atEpochMs = rawEntry["at_epoch_ms"];
    const rawTokens = rawEntry["tokens"];
    if (typeof kind !== "string" || !DIAGNOSTIC_KIND_SET.has(kind)) {
      return null;
    }
    const closedKind = kind;
    if (typeof atEpochMs !== "number" || !Number.isInteger(atEpochMs) || atEpochMs < 0) {
      return null;
    }
    if (!Array.isArray(rawTokens) || rawTokens.length > MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY) {
      return null;
    }
    const tokens = [];
    for (const rawToken of rawTokens) {
      const token = parseTrailToken(rawToken);
      if (token === null) {
        return null;
      }
      tokens.push(token);
    }
    entries.push({ kind: closedKind, atEpochMs, tokens });
  }
  return entries;
}
function toArrayBuffer(bytes) {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength
  );
}
var SyncDiagnosticsTrailImpl = class {
  #fileStore;
  #nowEpochMs;
  #entries = [];
  #appendFailureCount = 0;
  #hasPendingPersist = false;
  #appendDrain = null;
  /** Whether the current failure episode already recorded its marker entry. */
  #hasRecordedPersistFailureSinceLastSuccess = false;
  constructor(options) {
    this.#fileStore = options.fileStore;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  }
  async load() {
    let isPresent;
    try {
      isPresent = await this.#fileStore.exists(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME);
    } catch {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    if (!isPresent) {
      return;
    }
    let bytes;
    try {
      bytes = new Uint8Array(await this.#fileStore.readBinary(SYNC_DIAGNOSTICS_TRAIL_FILE_NAME));
    } catch {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    const parsed = parseTrailSidecar(bytes);
    if (parsed === null) {
      await this.#resetAfterUnreadableSidecar();
      return;
    }
    this.#entries = parsed.slice(-MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES).map((entry) => ({ ...entry }));
  }
  append(input) {
    this.#entries.push({
      kind: input.kind,
      atEpochMs: this.#nowEpochMs(),
      tokens: input.tokens.slice(0, MAX_SYNC_DIAGNOSTICS_TRAIL_TOKENS_PER_ENTRY)
    });
    if (this.#entries.length > MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES) {
      this.#entries.splice(0, this.#entries.length - MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES);
    }
    this.#hasPendingPersist = true;
    if (this.#appendDrain === null) {
      this.#appendDrain = this.#drainPendingPersists();
    }
    return this.#appendDrain;
  }
  readEntries() {
    return this.#entries.map((entry) => ({ ...entry, tokens: [...entry.tokens] }));
  }
  readAppendFailureCount() {
    return this.#appendFailureCount;
  }
  /** Reset to empty and durably record the reset; never throws. */
  #resetAfterUnreadableSidecar() {
    this.#entries = [];
    return this.append({ kind: "trail_reset", tokens: [] });
  }
  /**
   * The single serialized persist loop: one write at a time, and appends
   * that arrive while a write is in flight coalesce into the next one.
   */
  async #drainPendingPersists() {
    try {
      while (this.#hasPendingPersist) {
        this.#hasPendingPersist = false;
        try {
          await this.#fileStore.writeBinary(
            SYNC_DIAGNOSTICS_TRAIL_FILE_NAME,
            toArrayBuffer(this.#serializeEntries())
          );
          this.#hasRecordedPersistFailureSinceLastSuccess = false;
        } catch {
          this.#appendFailureCount = Math.min(
            MAX_SYNC_DIAGNOSTICS_TRAIL_APPEND_FAILURES,
            this.#appendFailureCount + 1
          );
          this.#recordPersistFailureMarkerEntry();
        }
      }
    } finally {
      this.#appendDrain = null;
    }
  }
  /**
   * Record ONE bounded `self_check` trail entry carrying the closed
   * `trail_persist_failed` verdict token per failure episode (child six
   * deferred remediation): the counter alone is invisible until a surface
   * reads it, and at 999 saturation it cannot move at all, so the marker
   * keeps every swallowed persist failure readable on the trail surfaces
   * even when no self-check command ran. The marker enters the ring
   * directly — recording it must not trigger another persist attempt or
   * bump the counter a second time — so it rides the NEXT successful
   * persist into the sidecar (an honest durable record) and re-arms only
   * after that success. Bounded by the ring's eviction cap like any entry.
   */
  #recordPersistFailureMarkerEntry() {
    if (this.#hasRecordedPersistFailureSinceLastSuccess) {
      return;
    }
    this.#hasRecordedPersistFailureSinceLastSuccess = true;
    this.#entries.push({
      kind: "self_check",
      atEpochMs: this.#nowEpochMs(),
      tokens: ["trail_persist_failed"]
    });
    if (this.#entries.length > MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES) {
      this.#entries.splice(0, this.#entries.length - MAX_SYNC_DIAGNOSTICS_TRAIL_ENTRIES);
    }
  }
  #serializeEntries() {
    const record = {
      contract: SYNC_DIAGNOSTICS_TRAIL_CONTRACT,
      entries: this.#entries.map((entry) => ({
        kind: entry.kind,
        at_epoch_ms: entry.atEpochMs,
        tokens: entry.tokens.map(
          (token) => typeof token === "string" ? token : { request_id: token.requestId }
        )
      }))
    };
    return new TextEncoder().encode(JSON.stringify(record));
  }
};
function createSyncDiagnosticsTrail(options) {
  return new SyncDiagnosticsTrailImpl(options);
}

// src/journal/queue-driver.ts
var QUEUE_PASS_DEADLINE_MS = 6e4;
var QUEUE_REQUEST_TIMEOUT_MS = 3e4;
var RETRY_BACKOFF_INITIAL_MS = 1e3;
var RETRY_BACKOFF_MAXIMUM_MS = 3e5;
var RETRY_BACKOFF_JITTER_FRACTION = 0.25;
function computeRetryBackoffMs(attemptCount, randomJitter) {
  if (!Number.isInteger(attemptCount) || attemptCount < 1) {
    throw new TypeError("attempt count must be a positive integer");
  }
  const exponent = Math.min(attemptCount - 1, 30);
  const exponentialDelayMs = Math.min(
    RETRY_BACKOFF_MAXIMUM_MS,
    RETRY_BACKOFF_INITIAL_MS * 2 ** exponent
  );
  const jitterMs = exponentialDelayMs * RETRY_BACKOFF_JITTER_FRACTION * randomJitter();
  return Math.min(RETRY_BACKOFF_MAXIMUM_MS, Math.round(exponentialDelayMs + jitterMs));
}
var MAX_JOURNAL_FAILURE_REASON_HISTORY = 5;
function syncFailureKind(error) {
  return error instanceof SyncApiError ? error.kind : null;
}
function journalEventStateToken(state) {
  switch (state) {
    case "queued":
      return "state_queued";
    case "waiting_retry":
      return "state_waiting_retry";
    case "preflight":
      return "state_preflight";
    case "uploading":
      return "state_uploading";
    case "blocked_conflict":
      return "state_blocked_conflict";
    case "excluded_policy":
      return "state_excluded_policy";
    case "blocked_size":
      return "state_blocked_size";
    case "deferred_lifecycle":
      return "state_deferred_lifecycle";
    case "integrity_failed":
      return "state_integrity_failed";
    case "committed":
      return "state_committed";
    case "no_change":
      return "state_no_change";
  }
}
function parkFailureSiteToken(eventId, safeError, nextEligibleRetryEpochMs) {
  const areParkArgumentsValid = isUuid4(eventId) && JOURNAL_SAFE_ERROR_LABELS.includes(safeError) && Number.isInteger(nextEligibleRetryEpochMs) && nextEligibleRetryEpochMs >= 0;
  return areParkArgumentsValid ? "site_mutation_internal" : "site_argument_validation";
}
var JournalQueueDriver = class {
  #repository;
  #syncApi;
  #fileBytesReader;
  #lifecycleDriver;
  /**
   * The active pass's abort scope, replaced per pass: a stall recovery (or
   * stop) aborts exactly the pass it targets, and any late result of an
   * abandoned pass observes its own aborted scope instead of the fresh
   * pass's controller.
   */
  #activePassController = null;
  /** When the running pass started (fake-clock friendly), for the stall bound. */
  #passStartedAtEpochMs = null;
  #refreshAccessToken;
  #nowEpochMs;
  #createCorrelationId;
  #randomJitter;
  #passDeadlineMs;
  #requestTimeoutMs;
  #diagnosticTrail;
  #multipartRunner;
  #multipartPlatform;
  /** The pass's last successful request outcome's envelope request id. */
  #lastPassWireRequestId = null;
  #isStopped = false;
  #isPassRunning = false;
  #journalFailureReasons = [];
  constructor(options) {
    this.#repository = options.repository;
    this.#syncApi = options.syncApi;
    this.#fileBytesReader = options.fileBytesReader;
    this.#lifecycleDriver = options.lifecycleDriver ?? null;
    this.#refreshAccessToken = options.refreshAccessToken;
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#createCorrelationId = options.createCorrelationId ?? (() => crypto.randomUUID());
    this.#randomJitter = options.randomJitter ?? (() => Math.random());
    this.#passDeadlineMs = options.passDeadlineMs ?? QUEUE_PASS_DEADLINE_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? QUEUE_REQUEST_TIMEOUT_MS;
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
    this.#multipartPlatform = options.multipartPlatform ?? "mobile";
    this.#multipartRunner = new MultipartUploadRunner({
      repository: options.repository,
      syncApi: options.syncApi,
      fileBytesReader: options.fileBytesReader,
      nowEpochMs: this.#nowEpochMs,
      requestTimeoutMs: this.#requestTimeoutMs,
      // The multipart lane's closed `multipart_failure` entries (multipart
      // task 11) ride the same durable trail as the driver's own entries.
      diagnosticTrail: this.#diagnosticTrail ?? void 0
    });
  }
  /** Whether the driver was stopped for unload/suspension and runs nothing new. */
  get isStopped() {
    return this.#isStopped;
  }
  /**
   * Whether outbound dispatch is paused by an active repair barrier
   * (task 11, spec 12.1): a reconciliation run freezes observation
   * generation G and holds every outbound row until the completion
   * releases them. An unreadable reconciliation state fails CLOSED — no
   * dispatch may race an unknown barrier — and the closed store reason
   * surfaces through the existing bounded ring and `journal_failure`
   * trail entry instead of being swallowed.
   */
  #isOutboundDispatchPaused() {
    try {
      return this.#repository.deviceSync.readState().barrierGeneration !== null;
    } catch (error) {
      this.#recordJournalFailureReason(error);
      this.#recordJournalFailureTrailEntry(error);
      return true;
    }
  }
  /**
   * Stop the driver (plugin unload / mobile suspension): no new pass
   * starts, and any in-flight `requestUrl` result arriving afterwards is
   * discarded rather than applied to the journal. The pass-scoped
   * AbortController is aborted so the lifecycle lane (when wired in)
   * also cancels cleanly.
   */
  stop() {
    this.#isStopped = true;
    this.#activePassController?.abort();
  }
  /**
   * The foreground trigger entry (load, Vault event, `Sync now`): runs one
   * bounded pass unless the driver is stopped or a pass is already active —
   * a trigger never queues a second pass and never recurses.
   */
  async requestPass() {
    if (this.#isStopped) {
      return { outcome: "stopped", processedEventCount: 0 };
    }
    if (this.#isPassRunning) {
      const startedAtEpochMs = this.#passStartedAtEpochMs;
      if (startedAtEpochMs === null || this.#nowEpochMs() - startedAtEpochMs < 2 * this.#passDeadlineMs) {
        return { outcome: "pass_already_running", processedEventCount: 0 };
      }
      this.#activePassController?.abort();
      this.#activePassController = null;
      this.#passStartedAtEpochMs = null;
      this.#isPassRunning = false;
      this.#diagnosticTrail?.append({
        kind: "pass_outcome",
        tokens: ["pass_stall_recovered"]
      });
    }
    return this.runPass();
  }
  /**
   * Run one bounded pass: the oldest eligible event at a time, one active
   * content request, until the queue drains, the deadline passes, login is
   * required, a retryable failure ends the pass with `retry_scheduled`, or
   * the driver stops. When a lifecycle driver is wired in, the pass
   * interleaves: it first drains the lifecycle lane to IDLE (spec 19.2
   * predecessor rule, task 9 fix round 1 I3), then processes one content
   * event. The two lanes never have an active mutating request in flight at
   * the same time, and the content lane never sees a lifecycle event
   * because the lane filter is enforced by draining the lifecycle lane
   * before each content selection.
   */
  async runPass() {
    if (this.#isStopped) {
      return { outcome: "stopped", processedEventCount: 0 };
    }
    if (this.#isPassRunning) {
      return { outcome: "pass_already_running", processedEventCount: 0 };
    }
    this.#isPassRunning = true;
    this.#lastPassWireRequestId = null;
    const passAbortController = new AbortController();
    this.#activePassController = passAbortController;
    this.#passStartedAtEpochMs = this.#nowEpochMs();
    const passDeadlineEpochMs = this.#nowEpochMs() + this.#passDeadlineMs;
    const refreshBudget = { hasRefreshed: false, requiresLogin: false };
    let processedEventCount = 0;
    let passOutcome = "completed";
    let passEndReason = null;
    try {
      while (!this.#isStopped && this.#nowEpochMs() < passDeadlineEpochMs) {
        if (this.#isOutboundDispatchPaused()) {
          break;
        }
        let lifecycleLoginRequired = false;
        if (this.#lifecycleDriver !== null && !this.#isStopped) {
          let lifecycleOutcome = "idle";
          do {
            try {
              lifecycleOutcome = await this.#lifecycleDriver.runOne(
                passAbortController.signal
              );
            } catch {
              break;
            }
            if (this.#isStopped || this.#nowEpochMs() >= passDeadlineEpochMs) {
              break;
            }
            if (lifecycleOutcome === "login_required") {
              if (refreshBudget.hasRefreshed) {
                lifecycleLoginRequired = true;
                break;
              }
              refreshBudget.hasRefreshed = true;
              let didRefresh = false;
              try {
                await this.#refreshAccessToken();
                didRefresh = true;
              } catch {
                refreshBudget.requiresLogin = true;
                this.#recordCredentialRefreshFailureTrailEntry();
              }
              if (this.#isStopped) {
                break;
              }
              if (!didRefresh) {
                lifecycleLoginRequired = true;
                break;
              }
              try {
                lifecycleOutcome = await this.#lifecycleDriver.runOne(
                  passAbortController.signal
                );
              } catch {
                break;
              }
              if (this.#isStopped || this.#nowEpochMs() >= passDeadlineEpochMs) {
                break;
              }
              if (lifecycleOutcome === "login_required") {
                lifecycleLoginRequired = true;
                break;
              }
            }
          } while (lifecycleOutcome === "committed");
          if (this.#isStopped) {
            break;
          }
          if (lifecycleLoginRequired) {
            passEndReason = "end_login_required";
            break;
          }
        }
        let continuation;
        try {
          const event = this.#repository.readOldestEligibleEvent(this.#nowEpochMs());
          if (event === null) {
            break;
          }
          continuation = await this.#processEvent(
            event,
            passDeadlineEpochMs,
            refreshBudget,
            passAbortController
          );
        } catch (error) {
          this.#recordJournalFailureReason(error);
          this.#recordJournalFailureTrailEntry(error);
          passEndReason = "end_journal_failure";
          break;
        }
        processedEventCount += 1;
        if (continuation !== "continue") {
          passEndReason = continuation;
          break;
        }
      }
      if (this.#isStopped) {
        passOutcome = "stopped";
      } else {
        switch (passEndReason) {
          case "end_login_required":
            passOutcome = "login_required";
            break;
          case "end_retry_scheduled":
            passOutcome = "retry_scheduled";
            break;
          case "end_journal_failure":
            passOutcome = refreshBudget.requiresLogin ? "login_required" : "completed";
            break;
          case "end_deadline_boundary":
            passOutcome = this.#hasEligibleEventNow() ? "deadline_reached" : "completed";
            break;
          case null:
            if (this.#nowEpochMs() >= passDeadlineEpochMs && this.#hasEligibleEventNow()) {
              passOutcome = "deadline_reached";
            }
            break;
          case "end_stopped":
            passOutcome = "stopped";
            break;
        }
      }
      return { outcome: passOutcome, processedEventCount };
    } finally {
      this.#recordPassOutcomeTrailEntry(passOutcome);
      if (this.#activePassController === passAbortController) {
        this.#activePassController = null;
        this.#passStartedAtEpochMs = null;
      }
      this.#isPassRunning = false;
    }
  }
  /**
   * The bounded closed-token view of the journal failures the pass loop's
   * fail-closed catch swallowed (fix round 5). Newest last; at most
   * {@link MAX_JOURNAL_FAILURE_REASON_HISTORY} tokens; in-memory only.
   */
  readJournalFailureReasons() {
    return [...this.#journalFailureReasons];
  }
  /** Record one swallowed journal failure's closed reason, if it has one. */
  #recordJournalFailureReason(error) {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    this.#journalFailureReasons.push(error.reason);
    if (this.#journalFailureReasons.length > MAX_JOURNAL_FAILURE_REASON_HISTORY) {
      this.#journalFailureReasons.shift();
    }
  }
  /**
   * Append one `journal_failure` trail entry with the swallowed store
   * error's closed reason (sync error tracing task 1). Closed tokens only;
   * fire-and-forget, never blocking the pass.
   */
  #recordJournalFailureTrailEntry(error) {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    void this.#diagnosticTrail?.append({ kind: "journal_failure", tokens: [error.reason] });
  }
  /**
   * Append one failure trail entry for one failed wire request outcome
   * that reached the failure hook (sync error tracing task 1). Trail v2
   * taxonomy (task 7): a credential absence BEFORE any transport contact —
   * the sync client's pre-contact `login_required` rejection — records the
   * `credential_failure` kind with the closed `access_missing` stage; it is
   * never a wire failure, because no HTTP attempt reached the transport.
   * Every other `SyncApiError` keeps the `wire_failure` kind: the closed
   * failure kind, plus the failing envelope's opaque request id when the
   * server sent a CANONICAL UUID (the trail's constructor gate nulls any
   * non-conforming value — the rejected value records nothing). A local
   * (non-wire) failure records nothing. Diagnostic round U1: when the
   * failing body parsed as the canonical envelope, its closed server error
   * code rides along as one additional closed token between the kind and
   * the request id — whitelisted at the trail boundary against the declared
   * runtime vocabulary, so a null code (an edge HTML body), a foreign code,
   * or a non-conforming code records nothing extra.
   */
  #recordWireFailureTrailEntry(error) {
    if (this.#diagnosticTrail === null || !(error instanceof SyncApiError)) {
      return;
    }
    if (error.isCredentialAbsent) {
      void this.#diagnosticTrail.append({
        kind: "credential_failure",
        tokens: ["access_missing", error.kind]
      });
      return;
    }
    const tokens = [error.kind];
    if (error.wireErrorCode !== null) {
      const errorCodeToken = envelopeErrorCode(error.wireErrorCode);
      if (errorCodeToken !== null) {
        tokens.push(errorCodeToken);
      }
    }
    if (error.requestId !== null) {
      const requestIdToken = envelopeRequestId(error.requestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
    void this.#diagnosticTrail.append({ kind: "wire_failure", tokens });
  }
  /**
   * Append one `credential_failure` trail entry for a failed credential
   * refresh (trail v2 taxonomy, task 7): the refresh seam threw before any
   * retried transport contact, so the swallowed failure surfaces as the
   * closed `refresh_failed` stage instead of disappearing into the pass's
   * login verdict. Fire-and-forget, never blocking the pass.
   */
  #recordCredentialRefreshFailureTrailEntry() {
    void this.#diagnosticTrail?.append({
      kind: "credential_failure",
      tokens: ["refresh_failed", "login_required"]
    });
  }
  /**
   * Append one `pass_outcome` trail entry (sync error tracing task 1): the
   * closed pass outcome plus the sampled request id of the pass's last
   * successful request outcome, when the server sent a canonical UUID (a
   * non-conforming sampled value is omitted by the trail's constructor
   * gate).
   */
  #recordPassOutcomeTrailEntry(outcome) {
    if (this.#diagnosticTrail === null) {
      return;
    }
    const tokens = [outcome];
    if (this.#lastPassWireRequestId !== null) {
      const requestIdToken = envelopeRequestId(this.#lastPassWireRequestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
    void this.#diagnosticTrail.append({ kind: "pass_outcome", tokens });
  }
  /**
   * Fail-closed eligibility re-probe for the deadline conversion: the pass
   * may report `deadline_reached` only when an eligible event still remains.
   * A throwing journal means no follow-up pass, and the probe never escapes
   * `runPass`.
   */
  #hasEligibleEventNow() {
    try {
      return this.#repository.readOldestEligibleEvent(this.#nowEpochMs()) !== null;
    } catch {
      return false;
    }
  }
  // --- one event -------------------------------------------------------------------------------------
  async #processEvent(event, passDeadlineEpochMs, refreshBudget, passAbortController) {
    const correlationId = this.#createCorrelationId();
    try {
      if (event.state === "queued" || event.state === "waiting_retry") {
        await this.#repository.markEventPreflightStarted(event.eventId);
      }
      const outcome = await this.#requestWithDeadline(
        () => this.#sendPreflight(event),
        passDeadlineEpochMs,
        refreshBudget
      );
      if (this.#isStopped) {
        return "end_stopped";
      }
      switch (outcome.outcome) {
        case "excluded":
          await this.#closeTerminal(event.eventId, "excluded_policy", "excluded_policy", correlationId);
          return "continue";
        case "conflict":
          return await this.#retainConflictCandidate(
            event,
            outcome,
            passDeadlineEpochMs,
            refreshBudget,
            correlationId
          );
        case "committed_replay":
          await this.#persistCommittedReceipt(event.eventId, outcome.receipt);
          return "continue";
        case "no_change":
          await this.#persistNoChangeReceipt(event.eventId, outcome.receipt);
          return "continue";
        case "single_part_upload":
          return await this.#streamContent(event, outcome.operationId, passDeadlineEpochMs, refreshBudget, correlationId);
        case "multipart_upload":
          return await this.#dispatchMultipartUpload(
            event,
            passDeadlineEpochMs,
            refreshBudget,
            correlationId,
            passAbortController
          );
      }
    } catch (error) {
      if (this.#isStopped) {
        return "end_stopped";
      }
      let failure = error;
      const resumeOperationId = this.#claimedResumeOperationId(event, error);
      if (resumeOperationId !== null) {
        try {
          return await this.#streamContent(
            event,
            resumeOperationId,
            passDeadlineEpochMs,
            refreshBudget,
            correlationId
          );
        } catch (resumeError) {
          if (this.#isStopped) {
            return "end_stopped";
          }
          failure = resumeError;
        }
      }
      const continuation = await this.#handleFailure(event.eventId, failure, correlationId, refreshBudget);
      return continuation;
    }
  }
  #claimedResumeOperationId(event, error) {
    if (!(error instanceof SyncApiError) || error.kind !== "operation_retry_required" || !error.canResumeClaimedOperation || event.state !== "uploading" && event.state !== "waiting_retry" || event.operationId === null) {
      return null;
    }
    const persisted = this.#repository.readEvent(event.eventId);
    return persisted?.operationId === event.operationId ? event.operationId : null;
  }
  /**
   * Dispatch one `multipart_upload` preflight outcome through the
   * resumable multipart runner (child 7 spec 4.3): the runner resumes the
   * durable session, drives only unfinished parts and either returns a
   * frozen receipt/local verdict or throws the SAME closed failure kinds
   * the single-part path already maps below. The one-per-pass credential
   * budget covers this lane exactly like the content stream: a first
   * `access_expired` rotates once and retries the run a single time.
   */
  async #dispatchMultipartUpload(event, passDeadlineEpochMs, refreshBudget, correlationId, passAbortController) {
    if (this.#isPastDeadline(passDeadlineEpochMs)) {
      return "end_deadline_boundary";
    }
    const runContext = {
      signal: passAbortController.signal,
      passDeadlineEpochMs
    };
    const issue = () => this.#multipartRunner.run(event, this.#multipartPlatform, runContext);
    let outcome;
    try {
      outcome = await issue();
    } catch (error) {
      const kind = syncFailureKind(error);
      if (kind !== "access_expired" || refreshBudget.hasRefreshed) {
        if (this.#isStopped) {
          return "end_stopped";
        }
        return await this.#handleFailure(event.eventId, error, correlationId, refreshBudget);
      }
      refreshBudget.hasRefreshed = true;
      try {
        await this.#refreshAccessToken();
      } catch {
        refreshBudget.requiresLogin = true;
        this.#recordCredentialRefreshFailureTrailEntry();
        if (this.#isStopped) {
          return "end_stopped";
        }
        return await this.#handleFailure(event.eventId, error, correlationId, refreshBudget);
      }
      if (this.#isStopped) {
        return "end_stopped";
      }
      try {
        outcome = await issue();
      } catch (retryError) {
        if (this.#isStopped) {
          return "end_stopped";
        }
        return await this.#handleFailure(event.eventId, retryError, correlationId, refreshBudget);
      }
    }
    if (this.#isStopped) {
      return "end_stopped";
    }
    this.#sampleSuccessfulWireRequestId();
    switch (outcome.outcome) {
      case "committed":
        await this.#persistCommittedReceipt(event.eventId, outcome.receipt);
        return "continue";
      case "no_change":
        await this.#persistNoChangeReceipt(event.eventId, outcome.receipt);
        return "continue";
      case "local_content_changed":
        await this.#closeTerminal(
          event.eventId,
          "integrity_failed",
          "multipart_local_content_changed",
          correlationId
        );
        return "continue";
      case "local_file_missing":
        return this.#handleIntentAwareLocalFileMissing(event.eventId, correlationId);
      case "pass_deadline_reached":
        return "end_deadline_boundary";
    }
  }
  /**
   * Retain one conflict verdict's candidate as durable conflict evidence
   * (child 8 spec 5.1). A same-identity replay (the stored conflict
   * identity) and a verdict without a grant park the event terminal as
   * `blocked_conflict`, exactly as before. A granted capture operation
   * uploads the event's still-frozen bytes through the conflict-content
   * route — never the publication route — and only then parks the event:
   * the conflict now lives server-side and the Inbox reaches it through
   * the Conflict API. Bytes that vanished or changed locally cannot become
   * evidence (the successor event owns the newer bytes), so the event parks
   * the same terminal way with no overwrite and no retry. A failed capture
   * upload never terminalizes as a network failure: the event keeps its
   * retry eligibility and the next same-identity preflight answers the
   * conflict again, while the server's capture replays idempotently by
   * event identity. The transport-ambiguous resume path stays on the
   * publication lane only — a capture grant never resumes through it.
   */
  async #retainConflictCandidate(event, outcome, passDeadlineEpochMs, refreshBudget, correlationId) {
    const operationId = outcome.operationId;
    if (outcome.conflictId !== null || operationId === null) {
      await this.#closeTerminal(
        event.eventId,
        "blocked_conflict",
        "blocked_conflict",
        correlationId
      );
      return "continue";
    }
    if (this.#isPastDeadline(passDeadlineEpochMs)) {
      return "end_deadline_boundary";
    }
    try {
      await this.#streamConflictCandidate(
        event,
        operationId,
        passDeadlineEpochMs,
        refreshBudget,
        correlationId
      );
    } catch (error) {
      if (this.#isStopped) {
        return "end_stopped";
      }
      return await this.#handleFailure(event.eventId, error, correlationId, refreshBudget);
    }
    if (this.#isStopped) {
      return "end_stopped";
    }
    await this.#closeTerminal(
      event.eventId,
      "blocked_conflict",
      "blocked_conflict",
      correlationId
    );
    return "continue";
  }
  async #streamConflictCandidate(event, operationId, passDeadlineEpochMs, refreshBudget, correlationId) {
    void correlationId;
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      return;
    }
    const contentBytes = await this.#readIntentAwareContentBytes(event, localFile);
    if (contentBytes === null) {
      return;
    }
    const currentFingerprint = await deriveFrozenFingerprint(contentBytes);
    if (currentFingerprint.sha256 !== event.fingerprint.sha256 || currentFingerprint.sizeBytes !== event.fingerprint.sizeBytes) {
      return;
    }
    await this.#requestWithDeadline(
      () => this.#syncApi.uploadSmallFileConflictCandidate({ operationId, contentBytes }),
      passDeadlineEpochMs,
      refreshBudget
    );
  }
  async #streamContent(event, operationId, passDeadlineEpochMs, refreshBudget, correlationId) {
    await this.#repository.markEventUploading(event.eventId, operationId);
    if (this.#isStopped) {
      return "end_stopped";
    }
    if (this.#isPastDeadline(passDeadlineEpochMs)) {
      return "end_deadline_boundary";
    }
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      return this.#handleIntentAwareLocalFileMissing(event.eventId, correlationId);
    }
    const contentBytes = await this.#readIntentAwareContentBytes(event, localFile);
    if (contentBytes === null) {
      return this.#handleIntentAwareLocalFileMissing(event.eventId, correlationId);
    }
    if (contentBytes.byteLength > MAX_FILE_SIZE_BYTES) {
      await this.#closeTerminal(event.eventId, "blocked_size", "blocked_size", correlationId);
      return "continue";
    }
    const currentFingerprint = await deriveFrozenFingerprint(contentBytes);
    if (currentFingerprint.sha256 !== event.fingerprint.sha256 || currentFingerprint.sizeBytes !== event.fingerprint.sizeBytes) {
      await this.#closeTerminal(event.eventId, "integrity_failed", "integrity_failed", correlationId);
      return "continue";
    }
    const receipt = await this.#requestWithDeadline(
      () => this.#syncApi.uploadSmallFileContent({ operationId, contentBytes }),
      passDeadlineEpochMs,
      refreshBudget
    );
    if (this.#isStopped) {
      return "end_stopped";
    }
    await this.#persistCommittedReceipt(event.eventId, receipt);
    return "continue";
  }
  // --- network seam -----------------------------------------------------------------------------------
  /**
   * Issue one request under the driver deadline: `requestUrl` cannot be
   * aborted, so the await races a timer and a late result after the timeout
   * (or after stop) is discarded rather than applied.
   */
  async #requestWithDeadline(issue, passDeadlineEpochMs, refreshBudget) {
    const timeoutMs = Math.max(
      1,
      Math.min(this.#requestTimeoutMs, passDeadlineEpochMs - this.#nowEpochMs())
    );
    let firstAttempt;
    try {
      firstAttempt = await this.#raceTimeout(issue(), timeoutMs);
    } catch (error) {
      const kind = syncFailureKind(error);
      if (kind !== "access_expired" || refreshBudget.hasRefreshed) {
        throw error;
      }
      refreshBudget.hasRefreshed = true;
      try {
        await this.#refreshAccessToken();
      } catch {
        refreshBudget.requiresLogin = true;
        this.#recordCredentialRefreshFailureTrailEntry();
        throw error;
      }
      if (this.#isStopped) {
        throw error;
      }
      const retried = await this.#raceTimeout(issue(), timeoutMs);
      this.#sampleSuccessfulWireRequestId();
      return retried;
    }
    this.#sampleSuccessfulWireRequestId();
    return firstAttempt;
  }
  /**
   * Sample the envelope request id of the request outcome that just
   * settled successfully (sync error tracing task 1). The pass holds at
   * most one active request, so the accessor is unambiguous here; failure
   * outcomes carry their own id on the thrown error instead.
   */
  #sampleSuccessfulWireRequestId() {
    this.#lastPassWireRequestId = this.#syncApi.readLastEnvelopeRequestId();
  }
  #raceTimeout(request, timeoutMs) {
    return new Promise((resolve, reject) => {
      let hasSettled = false;
      const timeoutHandle = setTimeout(() => {
        if (hasSettled) {
          return;
        }
        hasSettled = true;
        reject(new SyncApiError("network_timeout"));
      }, timeoutMs);
      request.then(
        (value) => {
          if (hasSettled) {
            return;
          }
          hasSettled = true;
          clearTimeout(timeoutHandle);
          resolve(value);
        },
        (error) => {
          if (hasSettled) {
            return;
          }
          hasSettled = true;
          clearTimeout(timeoutHandle);
          reject(error);
        }
      );
    });
  }
  async #sendPreflight(event) {
    const localFile = this.#requireLocalFile(event);
    const operation = localFile.sourceId === null ? "create" : "update";
    return this.#syncApi.preflightJournalEvent({
      eventId: event.eventId,
      idempotencyKey: event.idempotencyKey,
      operation,
      localFileId: event.localFileId,
      sourceId: operation === "update" ? localFile.sourceId : null,
      baseVersionId: operation === "update" ? localFile.baseVersionId : null,
      normalizedLocator: localFile.normalizedPath,
      fingerprint: event.fingerprint,
      policyRevisionNumber: localFile.policyRevisionNumber
    });
  }
  #requireLocalFile(event) {
    const localFile = this.#repository.readLocalFileByLocalFileId(event.localFileId);
    if (localFile === null) {
      throw new SyncApiError("server_error");
    }
    return localFile;
  }
  /**
   * Read bytes from the latest durable rename endpoint when this content
   * event's owner has an unresolved chain. Preflight intentionally keeps
   * using the local row's prior locator, preserving its frozen operation
   * identity; only the local byte read follows the user's latest move.
   */
  async #readIntentAwareContentBytes(event, localFile) {
    let intent;
    try {
      intent = this.#repository.lifecycle.readPendingRenameIntentForLocalFile(
        event.localFileId
      );
    } catch (error) {
      void this.#diagnosticTrail?.append({
        kind: "journal_failure",
        tokens: ["pending_rename_intent_read_failed"]
      });
      if (error instanceof JournalStoreError) {
        throw error;
      }
      throw journalStoreError("journal_query_failed");
    }
    return this.#fileBytesReader.readRegularFileBytes(intent?.currentPath ?? localFile.normalizedPath);
  }
  #isPastDeadline(passDeadlineEpochMs) {
    return this.#nowEpochMs() >= passDeadlineEpochMs;
  }
  // --- failure and receipt helpers ----------------------------------------------------------------------
  async #handleFailure(eventId, error, correlationId, refreshBudget) {
    const kind = syncFailureKind(error);
    this.#recordWireFailureTrailEntry(error);
    switch (kind) {
      case "access_expired":
      case "login_required":
        refreshBudget.requiresLogin = true;
        await this.#scheduleRetry(eventId, "login_required", correlationId);
        return "end_login_required";
      case "blocked_size":
        await this.#closeTerminal(eventId, "blocked_size", "blocked_size", correlationId);
        return "continue";
      case "blocked_conflict": {
        await this.#closeTerminal(eventId, "blocked_conflict", "blocked_conflict", correlationId);
        return "continue";
      }
      case "integrity_failed":
        await this.#closeTerminal(eventId, "integrity_failed", "integrity_failed", correlationId);
        return "continue";
      case "policy_denied":
        await this.#closeTerminal(eventId, "excluded_policy", "excluded_policy", correlationId);
        return "continue";
      case "network_offline":
      case "network_timeout":
      case "network_rate_limited":
      case "server_error":
      case "operation_retry_required":
      default: {
        const safeError = this.#safeRetryLabel(kind);
        await this.#scheduleRetry(eventId, safeError, correlationId);
        return "end_retry_scheduled";
      }
    }
  }
  /**
   * Route a vanished current endpoint through the one serialized intent-aware
   * resolver. The resolver owns audit/counter/event mutations; this driver
   * owns exactly one post-commit diagnostic and repair-barrier request.
   */
  async #handleIntentAwareLocalFileMissing(eventId, correlationId) {
    const resolution = await this.#repository.resolveIntentAwareLocalFileMissing({
      eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      requestCorrelationId: correlationId,
      nextEligibleRetryEpochMs: this.#nowEpochMs() + FILE_SETTLE_DELAY_MS
    });
    switch (resolution.outcome) {
      case "waiting_for_rename":
        return "end_retry_scheduled";
      case "closed_deferred_lifecycle":
        return "continue";
      case "reconcile_takeover":
        void this.#diagnosticTrail?.append({
          kind: "journal_failure",
          tokens: [resolution.diagnosticReason]
        });
        await this.#startRepairBarrier();
        return "continue";
    }
  }
  /** Start or retain the existing device repair barrier after a terminal ownership handoff. */
  async #startRepairBarrier() {
    try {
      const generation = await this.#repository.deviceSync.nextObservationGeneration();
      await this.#repository.deviceSync.startRepairBarrier({
        generation,
        reason: "device_manifest_target_occupied"
      });
    } catch {
    }
  }
  #safeRetryLabel(kind) {
    switch (kind) {
      case "network_offline":
        return "network_offline";
      case "network_timeout":
        return "network_timeout";
      case "network_rate_limited":
        return "network_rate_limited";
      default:
        return "server_error";
    }
  }
  async #scheduleRetry(eventId, safeError, correlationId) {
    const event = this.#repository.readEvent(eventId);
    if (event === null) {
      return;
    }
    await this.#repository.recordEventAttempt({
      eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: safeError,
      requestCorrelationId: correlationId
    });
    const nextAttemptCount = event.attemptCount + 1;
    const nextEligibleRetryEpochMs = this.#nowEpochMs() + computeRetryBackoffMs(nextAttemptCount, this.#randomJitter);
    try {
      await this.#repository.markEventWaitingRetry(eventId, safeError, nextEligibleRetryEpochMs);
    } catch (error) {
      this.#recordParkFailureTrailEntry(eventId, safeError, nextEligibleRetryEpochMs, error);
      throw error;
    }
  }
  /**
   * Append one `journal_failure` trail entry from the retry-park throw site
   * (sync error tracing park diagnosis round): the park error's closed store
   * reason — or the closed `reason_unknown` token for a non-store error —
   * plus the closed state token of the row read back AT the failure moment,
   * where a null or throwing read-back records `row_absent`, plus (diagnostic
   * round U2) the closed throw-site token derived by re-checking the park
   * arguments the repository was given. Closed tokens only; fire-and-forget,
   * never blocking the pass.
   */
  #recordParkFailureTrailEntry(eventId, safeError, nextEligibleRetryEpochMs, error) {
    if (this.#diagnosticTrail === null) {
      return;
    }
    const reasonToken = error instanceof JournalStoreError ? error.reason : "reason_unknown";
    void this.#diagnosticTrail.append({
      kind: "journal_failure",
      tokens: [
        reasonToken,
        this.#readEventStateTokenAtFailure(eventId),
        parkFailureSiteToken(eventId, safeError, nextEligibleRetryEpochMs)
      ]
    });
  }
  /** The parked row's closed state token read back at the failure moment. */
  #readEventStateTokenAtFailure(eventId) {
    try {
      const event = this.#repository.readEvent(eventId);
      if (event === null) {
        return "row_absent";
      }
      return journalEventStateToken(event.state);
    } catch {
      return "row_absent";
    }
  }
  async #closeTerminal(eventId, terminalState, safeError, correlationId) {
    await this.#repository.resolveIntentAwareContentTerminal({
      eventId,
      terminalState,
      safeError,
      attemptedAtEpochMs: this.#nowEpochMs(),
      requestCorrelationId: correlationId
    });
    if (terminalState === "blocked_conflict") {
      await this.#startRepairBarrier();
    }
  }
  async #persistCommittedReceipt(eventId, receipt) {
    await this.#repository.recordCommittedReceipt({
      eventId,
      sourceId: receipt.sourceId,
      baseVersionId: receipt.sourceVersionId
    });
    await this.#materializePendingRenameAfterContentReceipt(eventId);
  }
  async #persistNoChangeReceipt(eventId, receipt) {
    await this.#repository.recordNoChangeReceipt({
      eventId,
      sourceId: receipt.sourceId,
      baseVersionId: receipt.sourceVersionId
    });
    await this.#materializePendingRenameAfterContentReceipt(eventId);
  }
  /** Materialize the current durable rename endpoints only after identity receipt lands. */
  async #materializePendingRenameAfterContentReceipt(eventId) {
    const event = this.#repository.readEvent(eventId);
    if (event === null || event.operation !== "create" && event.operation !== "update") {
      return;
    }
    await this.#repository.lifecycle.recordPendingRenameLifecycleEvent(
      event.localFileId,
      event.fingerprint
    );
  }
};

// ../../node_modules/.pnpm/openapi-fetch@0.17.0/node_modules/openapi-fetch/dist/index.mjs
var PATH_PARAM_RE = /\{[^{}]+\}/g;
var supportsRequestInitExt = () => {
  return typeof process === "object" && Number.parseInt(process?.versions?.node?.substring(0, 2)) >= 18 && process.versions.undici;
};
function randomID() {
  return Math.random().toString(36).slice(2, 11);
}
function createClient(clientOptions) {
  let {
    baseUrl = "",
    Request: CustomRequest = globalThis.Request,
    fetch: baseFetch = globalThis.fetch,
    querySerializer: globalQuerySerializer,
    bodySerializer: globalBodySerializer,
    pathSerializer: globalPathSerializer,
    headers: baseHeaders,
    requestInitExt = void 0,
    ...baseOptions
  } = { ...clientOptions };
  requestInitExt = supportsRequestInitExt() ? requestInitExt : void 0;
  baseUrl = removeTrailingSlash(baseUrl);
  const globalMiddlewares = [];
  async function coreFetch(schemaPath, fetchOptions) {
    const {
      baseUrl: localBaseUrl,
      fetch: fetch2 = baseFetch,
      Request: Request2 = CustomRequest,
      headers,
      params = {},
      parseAs = "json",
      querySerializer: requestQuerySerializer,
      bodySerializer = globalBodySerializer ?? defaultBodySerializer,
      pathSerializer: requestPathSerializer,
      body,
      middleware: requestMiddlewares = [],
      ...init
    } = fetchOptions || {};
    let finalBaseUrl = baseUrl;
    if (localBaseUrl) {
      finalBaseUrl = removeTrailingSlash(localBaseUrl) ?? baseUrl;
    }
    let querySerializer = typeof globalQuerySerializer === "function" ? globalQuerySerializer : createQuerySerializer(globalQuerySerializer);
    if (requestQuerySerializer) {
      querySerializer = typeof requestQuerySerializer === "function" ? requestQuerySerializer : createQuerySerializer({
        ...typeof globalQuerySerializer === "object" ? globalQuerySerializer : {},
        ...requestQuerySerializer
      });
    }
    const pathSerializer = requestPathSerializer || globalPathSerializer || defaultPathSerializer;
    const serializedBody = body === void 0 ? void 0 : bodySerializer(
      body,
      // Note: we declare mergeHeaders() both here and below because it’s a bit of a chicken-or-egg situation:
      // bodySerializer() needs all headers so we aren’t dropping ones set by the user, however,
      // the result of this ALSO sets the lowest-priority content-type header. So we re-merge below,
      // setting the content-type at the very beginning to be overwritten.
      // Lastly, based on the way headers work, it’s not a simple “present-or-not” check becauase null intentionally un-sets headers.
      mergeHeaders(baseHeaders, headers, params.header)
    );
    const finalHeaders = mergeHeaders(
      // with no body, we should not to set Content-Type
      serializedBody === void 0 || // if serialized body is FormData; browser will correctly set Content-Type & boundary expression
      serializedBody instanceof FormData ? {} : {
        "Content-Type": "application/json"
      },
      baseHeaders,
      headers,
      params.header
    );
    const finalMiddlewares = [...globalMiddlewares, ...requestMiddlewares];
    const requestInit = {
      redirect: "follow",
      ...baseOptions,
      ...init,
      body: serializedBody,
      headers: finalHeaders
    };
    let id;
    let options;
    let request = new Request2(
      createFinalURL(schemaPath, { baseUrl: finalBaseUrl, params, querySerializer, pathSerializer }),
      requestInit
    );
    let response;
    for (const key in init) {
      if (!(key in request)) {
        request[key] = init[key];
      }
    }
    if (finalMiddlewares.length) {
      id = randomID();
      options = Object.freeze({
        baseUrl: finalBaseUrl,
        fetch: fetch2,
        parseAs,
        querySerializer,
        bodySerializer,
        pathSerializer
      });
      for (const m of finalMiddlewares) {
        if (m && typeof m === "object" && typeof m.onRequest === "function") {
          const result = await m.onRequest({
            request,
            schemaPath,
            params,
            options,
            id
          });
          if (result) {
            if (result instanceof Request2) {
              request = result;
            } else if (result instanceof Response) {
              response = result;
              break;
            } else {
              throw new Error("onRequest: must return new Request() or Response() when modifying the request");
            }
          }
        }
      }
    }
    if (!response) {
      try {
        response = await fetch2(request, requestInitExt);
      } catch (error2) {
        let errorAfterMiddleware = error2;
        if (finalMiddlewares.length) {
          for (let i = finalMiddlewares.length - 1; i >= 0; i--) {
            const m = finalMiddlewares[i];
            if (m && typeof m === "object" && typeof m.onError === "function") {
              const result = await m.onError({
                request,
                error: errorAfterMiddleware,
                schemaPath,
                params,
                options,
                id
              });
              if (result) {
                if (result instanceof Response) {
                  errorAfterMiddleware = void 0;
                  response = result;
                  break;
                }
                if (result instanceof Error) {
                  errorAfterMiddleware = result;
                  continue;
                }
                throw new Error("onError: must return new Response() or instance of Error");
              }
            }
          }
        }
        if (errorAfterMiddleware) {
          throw errorAfterMiddleware;
        }
      }
      if (finalMiddlewares.length) {
        for (let i = finalMiddlewares.length - 1; i >= 0; i--) {
          const m = finalMiddlewares[i];
          if (m && typeof m === "object" && typeof m.onResponse === "function") {
            const result = await m.onResponse({
              request,
              response,
              schemaPath,
              params,
              options,
              id
            });
            if (result) {
              if (!(result instanceof Response)) {
                throw new Error("onResponse: must return new Response() when modifying the response");
              }
              response = result;
            }
          }
        }
      }
    }
    const contentLength = response.headers.get("Content-Length");
    if (response.status === 204 || request.method === "HEAD" || contentLength === "0" && !response.headers.get("Transfer-Encoding")?.includes("chunked")) {
      return response.ok ? { data: void 0, response } : { error: void 0, response };
    }
    if (response.ok) {
      const getResponseData = async () => {
        if (parseAs === "stream") {
          return response.body;
        }
        if (parseAs === "json" && !contentLength) {
          const raw = await response.text();
          return raw ? JSON.parse(raw) : void 0;
        }
        return await response[parseAs]();
      };
      return { data: await getResponseData(), response };
    }
    let error = await response.text();
    try {
      error = JSON.parse(error);
    } catch {
    }
    return { error, response };
  }
  return {
    request(method, url, init) {
      return coreFetch(url, { ...init, method: method.toUpperCase() });
    },
    /** Call a GET endpoint */
    GET(url, init) {
      return coreFetch(url, { ...init, method: "GET" });
    },
    /** Call a PUT endpoint */
    PUT(url, init) {
      return coreFetch(url, { ...init, method: "PUT" });
    },
    /** Call a POST endpoint */
    POST(url, init) {
      return coreFetch(url, { ...init, method: "POST" });
    },
    /** Call a DELETE endpoint */
    DELETE(url, init) {
      return coreFetch(url, { ...init, method: "DELETE" });
    },
    /** Call a OPTIONS endpoint */
    OPTIONS(url, init) {
      return coreFetch(url, { ...init, method: "OPTIONS" });
    },
    /** Call a HEAD endpoint */
    HEAD(url, init) {
      return coreFetch(url, { ...init, method: "HEAD" });
    },
    /** Call a PATCH endpoint */
    PATCH(url, init) {
      return coreFetch(url, { ...init, method: "PATCH" });
    },
    /** Call a TRACE endpoint */
    TRACE(url, init) {
      return coreFetch(url, { ...init, method: "TRACE" });
    },
    /** Register middleware */
    use(...middleware) {
      for (const m of middleware) {
        if (!m) {
          continue;
        }
        if (typeof m !== "object" || !("onRequest" in m || "onResponse" in m || "onError" in m)) {
          throw new Error("Middleware must be an object with one of `onRequest()`, `onResponse() or `onError()`");
        }
        globalMiddlewares.push(m);
      }
    },
    /** Unregister middleware */
    eject(...middleware) {
      for (const m of middleware) {
        const i = globalMiddlewares.indexOf(m);
        if (i !== -1) {
          globalMiddlewares.splice(i, 1);
        }
      }
    }
  };
}
function serializePrimitiveParam(name, value, options) {
  if (value === void 0 || value === null) {
    return "";
  }
  if (typeof value === "object") {
    throw new Error(
      "Deeply-nested arrays/objects aren\u2019t supported. Provide your own `querySerializer()` to handle these."
    );
  }
  return `${name}=${options?.allowReserved === true ? value : encodeURIComponent(value)}`;
}
function serializeObjectParam(name, value, options) {
  if (!value || typeof value !== "object") {
    return "";
  }
  const values = [];
  const joiner = {
    simple: ",",
    label: ".",
    matrix: ";"
  }[options.style] || "&";
  if (options.style !== "deepObject" && options.explode === false) {
    for (const k in value) {
      values.push(k, options.allowReserved === true ? value[k] : encodeURIComponent(value[k]));
    }
    const final2 = values.join(",");
    switch (options.style) {
      case "form": {
        return `${name}=${final2}`;
      }
      case "label": {
        return `.${final2}`;
      }
      case "matrix": {
        return `;${name}=${final2}`;
      }
      default: {
        return final2;
      }
    }
  }
  for (const k in value) {
    const finalName = options.style === "deepObject" ? `${name}[${k}]` : k;
    values.push(serializePrimitiveParam(finalName, value[k], options));
  }
  const final = values.join(joiner);
  return options.style === "label" || options.style === "matrix" ? `${joiner}${final}` : final;
}
function serializeArrayParam(name, value, options) {
  if (!Array.isArray(value)) {
    return "";
  }
  if (options.explode === false) {
    const joiner2 = { form: ",", spaceDelimited: "%20", pipeDelimited: "|" }[options.style] || ",";
    const final = (options.allowReserved === true ? value : value.map((v) => encodeURIComponent(v))).join(joiner2);
    switch (options.style) {
      case "simple": {
        return final;
      }
      case "label": {
        return `.${final}`;
      }
      case "matrix": {
        return `;${name}=${final}`;
      }
      // case "spaceDelimited":
      // case "pipeDelimited":
      default: {
        return `${name}=${final}`;
      }
    }
  }
  const joiner = { simple: ",", label: ".", matrix: ";" }[options.style] || "&";
  const values = [];
  for (const v of value) {
    if (options.style === "simple" || options.style === "label") {
      values.push(options.allowReserved === true ? v : encodeURIComponent(v));
    } else {
      values.push(serializePrimitiveParam(name, v, options));
    }
  }
  return options.style === "label" || options.style === "matrix" ? `${joiner}${values.join(joiner)}` : values.join(joiner);
}
function createQuerySerializer(options) {
  return function querySerializer(queryParams) {
    const search = [];
    if (queryParams && typeof queryParams === "object") {
      for (const name in queryParams) {
        const value = queryParams[name];
        if (value === void 0 || value === null) {
          continue;
        }
        if (Array.isArray(value)) {
          if (value.length === 0) {
            continue;
          }
          search.push(
            serializeArrayParam(name, value, {
              style: "form",
              explode: true,
              ...options?.array,
              allowReserved: options?.allowReserved || false
            })
          );
          continue;
        }
        if (typeof value === "object") {
          search.push(
            serializeObjectParam(name, value, {
              style: "deepObject",
              explode: true,
              ...options?.object,
              allowReserved: options?.allowReserved || false
            })
          );
          continue;
        }
        search.push(serializePrimitiveParam(name, value, options));
      }
    }
    return search.join("&");
  };
}
function defaultPathSerializer(pathname, pathParams) {
  let nextURL = pathname;
  for (const match of pathname.match(PATH_PARAM_RE) ?? []) {
    let name = match.substring(1, match.length - 1);
    let explode = false;
    let style = "simple";
    if (name.endsWith("*")) {
      explode = true;
      name = name.substring(0, name.length - 1);
    }
    if (name.startsWith(".")) {
      style = "label";
      name = name.substring(1);
    } else if (name.startsWith(";")) {
      style = "matrix";
      name = name.substring(1);
    }
    if (!pathParams || pathParams[name] === void 0 || pathParams[name] === null) {
      continue;
    }
    const value = pathParams[name];
    if (Array.isArray(value)) {
      nextURL = nextURL.replace(match, serializeArrayParam(name, value, { style, explode }));
      continue;
    }
    if (typeof value === "object") {
      nextURL = nextURL.replace(match, serializeObjectParam(name, value, { style, explode }));
      continue;
    }
    if (style === "matrix") {
      nextURL = nextURL.replace(match, `;${serializePrimitiveParam(name, value)}`);
      continue;
    }
    nextURL = nextURL.replace(match, style === "label" ? `.${encodeURIComponent(value)}` : encodeURIComponent(value));
  }
  return nextURL;
}
function defaultBodySerializer(body, headers) {
  if (body instanceof FormData) {
    return body;
  }
  if (headers) {
    const contentType = headers.get instanceof Function ? headers.get("Content-Type") ?? headers.get("content-type") : headers["Content-Type"] ?? headers["content-type"];
    if (contentType === "application/x-www-form-urlencoded") {
      return new URLSearchParams(body).toString();
    }
  }
  return JSON.stringify(body);
}
function createFinalURL(pathname, options) {
  let finalURL = `${options.baseUrl}${pathname}`;
  if (options.params?.path) {
    finalURL = options.pathSerializer(finalURL, options.params.path);
  }
  let search = options.querySerializer(options.params.query ?? {});
  if (search.startsWith("?")) {
    search = search.substring(1);
  }
  if (search) {
    finalURL += `?${search}`;
  }
  return finalURL;
}
function mergeHeaders(...allHeaders) {
  const finalHeaders = new Headers();
  for (const h2 of allHeaders) {
    if (!h2 || typeof h2 !== "object") {
      continue;
    }
    const iterator = h2 instanceof Headers ? h2.entries() : Object.entries(h2);
    for (const [k, v] of iterator) {
      if (v === null) {
        finalHeaders.delete(k);
      } else if (Array.isArray(v)) {
        for (const v2 of v) {
          finalHeaders.append(k, v2);
        }
      } else if (v !== void 0) {
        finalHeaders.set(k, v);
      }
    }
  }
  return finalHeaders;
}
function removeTrailingSlash(url) {
  if (url.endsWith("/")) {
    return url.substring(0, url.length - 1);
  }
  return url;
}

// ../../packages/api-client/src/client.ts
function createApiClient(options) {
  return createClient({ baseUrl: options.baseUrl, fetch: options.transport });
}

// src/journal/lifecycle-api.ts
var LifecycleApiError = class extends Error {
  kind;
  label;
  /**
   * Whether the login rejection happened BEFORE any transport contact —
   * the resolved access credential was missing or empty (trail v2
   * taxonomy, task 7). Callers classify a marked error as
   * `credential_failure` on the diagnostics trail; a server-answerable
   * 401/403 keeps the flag false because an HTTP attempt reached the wire.
   */
  isCredentialAbsent;
  constructor(kind, label = null, isCredentialAbsent = false) {
    super(`lifecycle api failed: ${kind}`);
    this.name = "LifecycleApiError";
    this.kind = kind;
    this.label = label;
    this.isCredentialAbsent = isCredentialAbsent;
  }
};
function createRequestUrlLifecycleApi(options) {
  let cachedBaseUrl = null;
  let cachedClient = null;
  return buildLifecycleApi({
    resolveApiClient: () => {
      const baseUrl = options.resolveBaseUrl();
      if (cachedClient === null || baseUrl !== cachedBaseUrl) {
        cachedClient = createApiClient({
          baseUrl,
          transport: options.transport
        });
        cachedBaseUrl = baseUrl;
      }
      return cachedClient;
    },
    resolveAccessToken: options.resolveAccessToken
  });
}
var UUID_PATTERN10 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
var DATETIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$/;
var LIFECYCLE_5XX_INTEGRITY_CODES = /* @__PURE__ */ new Set([
  "source_commit_outcome_unknown",
  "source_idempotency_mismatch",
  "source_event_identity_mismatch",
  "source_verified_receipt_stale",
  "source_version_conflict",
  "source_content_object_conflict",
  "source_concurrency_invariant_failed",
  "canonical_recovery_integrity_failed",
  "canonical_recovery_restore_failed",
  "object_storage_integrity_failed",
  "object_storage_metadata_conflict"
]);
async function translate(result) {
  let envelope;
  try {
    envelope = await result;
  } catch {
    throw new LifecycleApiError("network_offline");
  }
  const status = envelope.response.status;
  if (status === 401 || status === 403) {
    throw new LifecycleApiError("login_required");
  }
  if (status === 409) {
    throw new LifecycleApiError("conflict");
  }
  if (status === 422) {
    throw new LifecycleApiError("integrity");
  }
  if (status === 429) {
    throw new LifecycleApiError("network_rate_limited");
  }
  if (status >= 500) {
    if (isIntegrity5xxEnvelope(envelope.data) || isIntegrity5xxEnvelope(envelope.error)) {
      throw new LifecycleApiError("integrity_5xx");
    }
    throw new LifecycleApiError("server_error");
  }
  if (!isRecord5(envelope.data)) {
    throw new LifecycleApiError("server_error");
  }
  const inner = envelope.data["data"];
  const error = envelope.data["error"];
  if (error !== null && error !== void 0) {
    throw new LifecycleApiError("server_error");
  }
  return parseLifecycleResult(inner);
}
function isIntegrity5xxEnvelope(parsedData) {
  if (!isRecord5(parsedData)) {
    return false;
  }
  const errorBody = parsedData["error"];
  if (!isRecord5(errorBody)) {
    return false;
  }
  const code = errorBody["code"];
  return typeof code === "string" && LIFECYCLE_5XX_INTEGRITY_CODES.has(code);
}
function isRecord5(value) {
  return typeof value === "object" && value !== null;
}
function parseLifecycleResult(data) {
  if (!isRecord5(data)) {
    throw new LifecycleApiError("server_error");
  }
  const {
    committed_at: committedAt,
    event_id: eventId,
    event_sequence: eventSequence,
    resulting_locator: resultingLocator,
    source_id: sourceId,
    source_version_id: sourceVersionId,
    state,
    tombstone_id: tombstoneId
  } = data;
  if (typeof committedAt !== "string" || !DATETIME_PATTERN.test(committedAt) || typeof eventId !== "string" || !UUID_PATTERN10.test(eventId) || typeof eventSequence !== "number" || !Number.isInteger(eventSequence) || eventSequence < 0 || resultingLocator !== null && typeof resultingLocator !== "string" || typeof sourceId !== "string" || !UUID_PATTERN10.test(sourceId) || typeof sourceVersionId !== "string" || !UUID_PATTERN10.test(sourceVersionId) || state !== "active" && state !== "deleted" || tombstoneId !== null && (typeof tombstoneId !== "string" || !UUID_PATTERN10.test(tombstoneId))) {
    throw new LifecycleApiError("server_error");
  }
  return {
    committedAt,
    eventId,
    eventSequence,
    resultingLocator,
    sourceId,
    sourceVersionId,
    state,
    tombstoneId
  };
}
function buildBody(event, tombstoneIdOverride) {
  const tombstoneId = event.operands.operation === "delete" ? null : tombstoneIdOverride !== void 0 ? tombstoneIdOverride : event.operands.tombstoneId;
  return {
    event_id: event.event.eventId,
    idempotency_key: event.event.idempotencyKey,
    source_id: event.operands.sourceId,
    operation: event.operands.operation,
    expected_version_id: event.operands.expectedVersionId,
    expected_locator: event.operands.expectedLocator,
    target_locator: event.operands.targetLocator,
    tombstone_id: tombstoneId,
    policy_revision: event.operands.policyRevision,
    client_timestamp: (/* @__PURE__ */ new Date()).toISOString()
  };
}
function buildLifecycleApi(options) {
  const { resolveApiClient, resolveAccessToken } = options;
  function bearerHeaders() {
    const accessToken = resolveAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      throw new LifecycleApiError("login_required", null, true);
    }
    return {
      authorization: `Bearer ${accessToken}`,
      accept: "application/json"
    };
  }
  return {
    async commit(event, signal, tombstoneIdOverride) {
      const headers = bearerHeaders();
      const body = buildBody(event, tombstoneIdOverride);
      const result = resolveApiClient().POST("/api/sources/lifecycle-events", {
        body,
        headers,
        // openapi-fetch accepts the standard RequestInit signal so an
        // upstream AbortController cuts off the in-flight HTTP request
        // before the response is mapped.
        ...signal !== void 0 ? { signal } : {}
      });
      return translate(result);
    }
  };
}

// src/journal/lifecycle-driver.ts
var RETRY_BACKOFF_INITIAL_MS2 = 1e3;
var RETRY_BACKOFF_MAXIMUM_MS2 = 3e5;
var RETRY_BACKOFF_JITTER_FRACTION2 = 0.25;
function computeLifecycleRetryBackoffMs(attemptCount, randomJitter) {
  if (!Number.isInteger(attemptCount) || attemptCount < 1) {
    throw new TypeError("attempt count must be a positive integer");
  }
  const exponent = Math.min(attemptCount - 1, 30);
  const exponentialDelayMs = Math.min(
    RETRY_BACKOFF_MAXIMUM_MS2,
    RETRY_BACKOFF_INITIAL_MS2 * 2 ** exponent
  );
  const jitterMs = exponentialDelayMs * RETRY_BACKOFF_JITTER_FRACTION2 * randomJitter();
  return Math.min(RETRY_BACKOFF_MAXIMUM_MS2, Math.round(exponentialDelayMs + jitterMs));
}
var LifecycleDriverImpl = class {
  #repository;
  #lifecycle;
  #api;
  #createCorrelationId;
  #randomJitter;
  #nowEpochMs;
  #diagnosticTrail;
  #onPendingRenameIntentReady;
  #disposeController;
  #isDisposed = false;
  constructor(options) {
    this.#repository = options.repository;
    this.#lifecycle = options.lifecycle;
    this.#api = options.api;
    this.#createCorrelationId = options.createCorrelationId ?? (() => crypto.randomUUID());
    this.#randomJitter = options.randomJitter ?? (() => Math.random());
    this.#nowEpochMs = options.nowEpochMs ?? (() => Date.now());
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
    this.#onPendingRenameIntentReady = options.onPendingRenameIntentReady ?? null;
    this.#disposeController = new AbortController();
  }
  /** Whether the driver was disposed for unload / suspension. */
  get isDisposed() {
    return this.#isDisposed;
  }
  /**
   * Halt the driver: every subsequent {@link runOne} returns `"idle"`
   * and any in-flight commit is aborted via the combined
   * `AbortSignal`.
   */
  dispose() {
    if (this.#isDisposed) {
      return;
    }
    this.#isDisposed = true;
    this.#disposeController.abort();
  }
  /**
   * Run one bounded pass over the lifecycle lane: select the oldest
   * eligible event, send it through the generated API client,
   * persist the server result, then return the closed outcome. The
   * call never throws; every thrown `LifecycleApiError` is mapped
   * onto the closed `{ outcome, label }` vocabulary so the queue
   * composition can keep moving.
   */
  async runOne(signal) {
    if (this.#isDisposed) {
      return "idle";
    }
    const combinedSignal = combineSignals(signal, this.#disposeController.signal);
    if (combinedSignal.aborted) {
      return "idle";
    }
    const frozen = this.#lifecycle.readOldestEligibleLifecycleEvent(this.#nowEpochMs());
    if (frozen === null) {
      return "idle";
    }
    if (combinedSignal.aborted) {
      return "idle";
    }
    const tombstoneIdOverride = this.#resolveRestoreTombstoneOverride(frozen);
    const correlationId = this.#createCorrelationId();
    let result;
    try {
      result = await this.#api.commit(frozen, combinedSignal, tombstoneIdOverride);
    } catch (error) {
      if (this.#isDisposed || combinedSignal.aborted) {
        return "idle";
      }
      return this.#mapApiError(frozen, error, correlationId);
    }
    if (this.#isDisposed || combinedSignal.aborted) {
      return "idle";
    }
    await this.#repository.recordEventAttempt({
      eventId: frozen.event.eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: "committed",
      requestCorrelationId: correlationId
    });
    const serverReceipt = result.tombstoneId === null ? null : { tombstoneId: result.tombstoneId };
    const receipt = await this.#lifecycle.recordLifecycleCommittedReceipt(
      frozen.event.eventId,
      serverReceipt
    );
    if (receipt.pendingRenameIntentLocalFileId !== null) {
      this.#onPendingRenameIntentReady?.(receipt.pendingRenameIntentLocalFileId);
    }
    if (frozen.operands.operation === "restore") {
      await this.#lifecycle.consumeRestoreSuccessor(frozen.event.localFileId);
    }
    return "committed";
  }
  /**
   * Resolve the tombstone id the wire body must carry for one
   * lifecycle event. A restore event with a server-receipt-bearing
   * delete predecessor MUST send the server-confirmed id; any other
   * operation (or a restore whose predecessor has no persisted server
   * receipt yet) falls through to the operands-derived value.
   */
  #resolveRestoreTombstoneOverride(frozen) {
    if (frozen.operands.operation !== "restore") {
      return void 0;
    }
    const predecessorId = frozen.operands.predecessorEventId;
    if (predecessorId === null) {
      return void 0;
    }
    const predecessorReceipt = this.#lifecycle.readServerReceiptTombstoneId(predecessorId);
    if (predecessorReceipt === null) {
      return void 0;
    }
    return predecessorReceipt;
  }
  // --- error mapping --------------------------------------------------------------------
  /**
   * Append ONE `credential_failure` trail entry when the login rejection
   * happened BEFORE any transport contact — the adapter's marked
   * missing-credential throw (trail v2 taxonomy, task 7). A
   * server-answerable 401/403 records nothing here: contact happened, so
   * the failure belongs to the wire taxonomy of the lanes that observed
   * it. Fire-and-forget, never blocking the dispatch.
   */
  #recordCredentialAbsenceTrailEntry(apiError) {
    if (this.#diagnosticTrail === null || !apiError.isCredentialAbsent) {
      return;
    }
    void this.#diagnosticTrail.append({
      kind: "credential_failure",
      tokens: ["access_missing", "login_required"]
    });
  }
  async #mapApiError(frozen, error, correlationId) {
    if (!(error instanceof LifecycleApiError)) {
      return "retry";
    }
    const apiError = error;
    switch (apiError.kind) {
      case "conflict":
        await this.#closeTerminal(frozen.event.eventId, "blocked_conflict", correlationId);
        return "blocked";
      case "integrity":
      case "integrity_5xx":
        await this.#closeTerminal(frozen.event.eventId, "integrity_failed", correlationId);
        return "blocked";
      case "login_required":
        this.#recordCredentialAbsenceTrailEntry(apiError);
        await this.#scheduleRetry(frozen.event.eventId, "login_required", correlationId);
        return "login_required";
      case "network_offline":
      case "network_timeout":
      case "network_rate_limited":
      case "server_error":
        await this.#scheduleRetry(frozen.event.eventId, retryLabelForKind(apiError.kind), correlationId);
        return "retry";
      default: {
        const _exhaustive = apiError.kind;
        void _exhaustive;
        return "retry";
      }
    }
  }
  async #closeTerminal(eventId, terminalState, correlationId) {
    let resolution;
    try {
      resolution = await this.#lifecycle.resolveIntentAwareLifecycleTerminal({
        eventId,
        terminalState,
        attemptedAtEpochMs: this.#nowEpochMs(),
        requestCorrelationId: correlationId
      });
    } catch (error) {
      void this.#diagnosticTrail?.append({
        kind: "journal_failure",
        tokens: ["lifecycle_reconcile_persist_failed"]
      });
      throw error;
    }
    if (resolution === "intent_reconciled") {
      void this.#diagnosticTrail?.append({
        kind: "journal_failure",
        tokens: ["pending_rename_intent_lifecycle_rejected"]
      });
      await this.#startRepairBarrier();
    }
  }
  /** Start or retain repair after the lifecycle resolver transfers locator ownership. */
  async #startRepairBarrier() {
    try {
      const generation = await this.#repository.deviceSync.nextObservationGeneration();
      await this.#repository.deviceSync.startRepairBarrier({
        generation,
        reason: "device_manifest_target_occupied"
      });
    } catch {
    }
  }
  async #scheduleRetry(eventId, safeError, correlationId) {
    const event = this.#repository.readEvent(eventId);
    if (event === null) {
      return;
    }
    await this.#repository.recordEventAttempt({
      eventId,
      attemptedAtEpochMs: this.#nowEpochMs(),
      outcomeLabel: safeError,
      requestCorrelationId: correlationId
    });
    const nextAttemptCount = event.attemptCount + 1;
    await this.#repository.markEventWaitingRetry(
      eventId,
      safeError,
      this.#nowEpochMs() + computeLifecycleRetryBackoffMs(nextAttemptCount, this.#randomJitter)
    );
  }
};
function retryLabelForKind(kind) {
  switch (kind) {
    case "network_offline":
      return "network_offline";
    case "network_timeout":
      return "network_timeout";
    case "network_rate_limited":
      return "network_rate_limited";
    case "server_error":
      return "server_error";
    // The integrity outcomes are non-retryable; the driver never
    // calls `retryLabelForKind` for them. The exhaustive `default`
    // branch keeps the closed-set pinning even if a future kind is
    // added without label coverage.
    case "integrity":
    case "integrity_5xx":
    case "conflict":
    case "login_required":
    default:
      return "server_error";
  }
}
function combineSignals(a, b) {
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([a, b]);
  }
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (a.aborted || b.aborted) {
    controller.abort();
    return controller.signal;
  }
  a.addEventListener("abort", onAbort, { once: true });
  b.addEventListener("abort", onAbort, { once: true });
  return controller.signal;
}

// src/journal/persistence.ts
var JOURNAL_GENERATION_FILE_PREFIX = "journal.sqlite.g";
var JOURNAL_MANIFEST_FILE_NAME = "journal.manifest.json";
var JOURNAL_MANIFEST_CONTRACT = "obsidian_journal_manifest/v1";
var MAX_BUFFERED_RECOVERY_PATHS = 1e3;
var SHA256_HEX_PATTERN2 = /^[0-9a-f]{64}$/;
var PENDING_RENAME_OPEN_EVENT_STATES_SQL = "'queued', 'preflight', 'uploading', 'waiting_retry'";
var PENDING_RENAME_UNMATERIALIZED_OWNER_STATE = "active";
function sqlText5(value) {
  return `'${value.replaceAll("'", "''")}'`;
}
function joinVaultPath(...segments) {
  return segments.flatMap((segment) => segment.split("/")).filter((segment) => segment.length > 0).join("/");
}
function createVaultPluginJournalStore(app, pluginId) {
  if (pluginId.trim().length === 0) {
    throw journalStoreError("journal_store_unavailable");
  }
  const { configDir, adapter } = app.vault;
  const pluginDirectory = joinVaultPath(configDir, "plugins", pluginId);
  const pluginDirectoryPrefix = `${pluginDirectory}/`;
  return {
    exists: (fileName) => adapter.exists(joinVaultPath(pluginDirectory, fileName)),
    readBinary: (fileName) => adapter.readBinary(joinVaultPath(pluginDirectory, fileName)),
    writeBinary: (fileName, data) => adapter.writeBinary(joinVaultPath(pluginDirectory, fileName), data),
    remove: (fileName) => adapter.remove(joinVaultPath(pluginDirectory, fileName)),
    list: async () => {
      const directory = await adapter.list(pluginDirectory);
      return directory.files.filter((path) => path.startsWith(pluginDirectoryPrefix)).map((path) => path.slice(pluginDirectoryPrefix.length));
    }
  };
}
function parseVerifiedGeneration(value) {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const candidate = value;
  const { generationNumber, sizeBytes, sha256, schemaVersion } = candidate;
  if (typeof generationNumber !== "number" || !Number.isInteger(generationNumber) || generationNumber < 1) {
    return null;
  }
  if (typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 1) {
    return null;
  }
  if (typeof sha256 !== "string" || !SHA256_HEX_PATTERN2.test(sha256)) {
    return null;
  }
  if (schemaVersion !== JOURNAL_SCHEMA_VERSION && schemaVersion !== RESTORE_RESERVATION_SCHEMA_VERSION && schemaVersion !== DEVICE_SYNC_SCHEMA_VERSION && schemaVersion !== MULTIPART_PROGRESS_SCHEMA_VERSION && schemaVersion !== CONFLICT_REPAIR_SCHEMA_VERSION) {
    return null;
  }
  return { generationNumber, sizeBytes, sha256, schemaVersion };
}
function parseJournalManifest(bytes) {
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const candidate = parsed;
  if (candidate["contract"] !== JOURNAL_MANIFEST_CONTRACT) {
    return null;
  }
  const current = parseVerifiedGeneration(candidate["current"]);
  if (current === null) {
    return null;
  }
  let prior = null;
  if (candidate["prior"] !== null && candidate["prior"] !== void 0) {
    prior = parseVerifiedGeneration(candidate["prior"]);
    if (prior === null) {
      return null;
    }
  }
  return { contract: JOURNAL_MANIFEST_CONTRACT, current, prior };
}
function isKnownJournalMigrationSource(schemaVersion) {
  return schemaVersion === RESTORE_RESERVATION_SCHEMA_VERSION || schemaVersion === DEVICE_SYNC_SCHEMA_VERSION || schemaVersion === MULTIPART_PROGRESS_SCHEMA_VERSION || schemaVersion === CONFLICT_REPAIR_SCHEMA_VERSION;
}
function isSameVerifiedGeneration(left, right) {
  return left.generationNumber === right.generationNumber && left.sizeBytes === right.sizeBytes && left.sha256 === right.sha256 && left.schemaVersion === right.schemaVersion;
}
function isSameJournalManifest(left, right) {
  if (!isSameVerifiedGeneration(left.current, right.current)) {
    return false;
  }
  if (left.prior === null || right.prior === null) {
    return left.prior === null && right.prior === null;
  }
  return isSameVerifiedGeneration(left.prior, right.prior);
}
function toArrayBuffer2(image) {
  return image.buffer.slice(
    image.byteOffset,
    image.byteOffset + image.byteLength
  );
}
function generationFileName(generationNumber) {
  return `${JOURNAL_GENERATION_FILE_PREFIX}${generationNumber}`;
}
function isGenerationFileName(fileName) {
  if (!fileName.startsWith(JOURNAL_GENERATION_FILE_PREFIX)) {
    return false;
  }
  const generationDigits = fileName.slice(JOURNAL_GENERATION_FILE_PREFIX.length);
  return /^[1-9][0-9]*$/.test(generationDigits);
}
var MAX_GENERATION_PUBLISH_FAILURE_REASONS = 5;
var JournalPersistence = class _JournalPersistence {
  #fileStore;
  #engineModule;
  #diagnosticTrail;
  #hasVaultContent;
  #database = null;
  #recoveryState = null;
  #verifiedGeneration = null;
  #priorVerifiedGeneration = null;
  #isReconcileRequired = false;
  /**
   * The reconcile-complete request of the device-sync repair completion
   * (task 11, spec 12.4): once the reconciler has proven the journal
   * healthy again, the sticky in-memory `#isReconcileRequired` view must
   * stop re-clobbering the repository-transaction clear of
   * `journal_meta.is_reconcile_required`. In reconcile-complete mode the
   * session meta is authoritative — a later queue-limit refusal that sets
   * the flag inside a transaction is still adopted, because the merge
   * reads the session meta AFTER the operation ran.
   */
  #isReconcileComplete = false;
  /**
   * Fix round 5 diagnostics: the bounded in-memory record of generation
   * PUBLISH failures (the file-store/publish path after a committed
   * transaction). Closed reason tokens only — the live torn-publish
   * investigation needed exactly this surface to discriminate
   * environmental write failures from code defects.
   */
  #generationPublishFailureCount = 0;
  #generationPublishFailureReasons = [];
  #hasRecoveryBufferOverflowed = false;
  #inFlightCommitCount = 0;
  #bufferedVaultPaths = /* @__PURE__ */ new Set();
  #commitTail = Promise.resolve();
  constructor(options) {
    this.#fileStore = options.fileStore;
    this.#engineModule = options.engineModule;
    this.#diagnosticTrail = options.diagnosticTrail ?? null;
    this.#hasVaultContent = options.hasVaultContent ?? null;
  }
  /**
   * Buffer one watcher path notification while recovery runs (spec 6.1).
   * Distinct paths coalesce; the buffer is bounded and one distinct path
   * beyond the bound flips the journal to `reconcile_required` durably
   * instead of silently losing the notification.
   */
  bufferVaultPathDuringRecovery(path) {
    if (this.#database !== null || this.#bufferedVaultPaths.has(path)) {
      return;
    }
    if (this.#bufferedVaultPaths.size >= MAX_BUFFERED_RECOVERY_PATHS) {
      this.#hasRecoveryBufferOverflowed = true;
      this.#isReconcileRequired = true;
      return;
    }
    this.#bufferedVaultPaths.add(path);
  }
  /** Take every buffered path once; the buffer stays empty afterwards. */
  drainBufferedVaultPaths() {
    const paths = [...this.#bufferedVaultPaths];
    this.#bufferedVaultPaths.clear();
    return paths;
  }
  get hasRecoveryBufferOverflowed() {
    return this.#hasRecoveryBufferOverflowed;
  }
  get isReconcileRequired() {
    return this.#isReconcileRequired;
  }
  /**
   * Request that the NEXT and every later generation commit honor a
   * repository-transaction clear of `journal_meta.is_reconcile_required`
   * (task 11, spec 12.4): the sticky merge defers to the session meta
   * instead of re-setting the in-memory view. The composition root wires
   * this as the `onDeviceSyncRepairComplete` callback of the journal
   * repository, so completing a device repair durably clears the flag.
   */
  markReconcileComplete() {
    this.#isReconcileComplete = true;
    this.#isReconcileRequired = false;
  }
  /**
   * Merge, never clobber — except across an explicit reconcile-complete
   * request: a repository that set `reconcile_required` inside this
   * transaction keeps the flag through the meta rewrite (spec 6.4), while
   * a proven-healthy journal's clear survives it (spec 12.4).
   */
  #mergeReconcileRequired(sessionMetaIsReconcileRequired) {
    if (this.#isReconcileComplete) {
      this.#isReconcileRequired = sessionMetaIsReconcileRequired;
      return this.#isReconcileRequired;
    }
    this.#isReconcileRequired ||= sessionMetaIsReconcileRequired;
    return this.#isReconcileRequired;
  }
  get recoveryState() {
    if (this.#recoveryState === null) {
      throw journalStoreError("journal_not_open");
    }
    return this.#recoveryState;
  }
  get verifiedGenerationNumber() {
    this.#requireOpenedDatabase();
    const verified = this.#verifiedGeneration;
    if (verified === null) {
      throw journalStoreError("journal_not_open");
    }
    return verified.generationNumber;
  }
  /** The current in-memory journal meta of the opened working database. */
  readJournalMeta() {
    return this.#requireOpenedDatabase().readJournalMeta();
  }
  /**
   * One read-only query on the opened working database (journal-scoped SQL
   * only). This is the narrow read seam a repository composition uses for
   * its queries: mutations still flow exclusively through
   * {@link commitGeneration}, so the single-writer invariant is untouched.
   */
  readAll(sql) {
    return this.#requireOpenedDatabase().readAll(sql);
  }
  /**
   * Run recovery (spec 6.2): accept only a manifest whose named generation
   * verifies, fall back to the newest prior verified generation, or rebuild
   * an empty `reconcile_required` journal when nothing verifies. Recovery
   * reads only journal-scoped files and never touches Vault content.
   */
  async open() {
    if (this.#database !== null) {
      return;
    }
    const { manifest, isManifestPresent } = await this.#readManifestState();
    const recovered = await this.#recoverVerifiedDatabase(manifest);
    if (recovered !== null) {
      const { database, verifiedGeneration, recoveryState, shouldPublishRecoveredImage } = recovered;
      this.#isReconcileRequired ||= database.readJournalMeta().isReconcileRequired;
      await this.#refreshRecoveredMeta(database, verifiedGeneration, recoveryState);
      this.#database = database;
      this.#recoveryState = recoveryState;
      this.#verifiedGeneration = verifiedGeneration;
      this.#priorVerifiedGeneration = manifest !== null && isSameVerifiedGeneration(manifest.current, verifiedGeneration) ? manifest.prior : null;
      if (shouldPublishRecoveredImage) {
        try {
          await this.#executeGenerationCommit(() => void 0);
        } catch (error) {
          this.close();
          throw error;
        }
      }
      return;
    }
    await this.#rebuildEmptyJournal(isManifestPresent);
  }
  /**
   * The single durable commit path: the operation's SQL transaction, the
   * generation export/verify/publish cycle and retention all run inside one
   * serialized queue, so concurrent commits produce strictly sequential
   * generations and a failed publish leaves the prior verified generation
   * intact.
   */
  async commitGeneration(operation) {
    this.#inFlightCommitCount += 1;
    const execution = this.#commitTail.then(() => this.#executeGenerationCommit(operation));
    this.#commitTail = execution.then(
      () => this.#finishTrackedCommit(),
      () => this.#finishTrackedCommit()
    );
    return execution;
  }
  #finishTrackedCommit() {
    this.#inFlightCommitCount = Math.max(0, this.#inFlightCommitCount - 1);
  }
  /**
   * The synchronous, bounded final-flush attempt of safe unload (spec 11):
   * every journal mutation already persisted its own verified generation,
   * so the attempt reports whether the journal sits at its final generation
   * or a commit is still in flight. It starts no work, awaits nothing, and
   * never blocks unload on async generation publishing — an interrupted
   * commit simply recovers from the newest verified generation on reopen.
   */
  attemptFinalFlush() {
    return this.#inFlightCommitCount > 0 ? "commit_in_flight" : "final_generation_current";
  }
  close() {
    this.#database?.close();
    this.#database = null;
    this.#recoveryState = null;
    this.#verifiedGeneration = null;
    this.#priorVerifiedGeneration = null;
  }
  #requireOpenedDatabase() {
    if (this.#database === null) {
      throw journalStoreError("journal_not_open");
    }
    return this.#database;
  }
  /**
   * The required table names that must be present on every verified
   * generation (spec 6.3, child 5; task 8 adds the five device-sync
   * tables of spec 8; task 9 adds the multipart progress table of child 7
   * spec 4.1; task 7 adds the conflict local repair table of Child 8
   * spec 5.2.6/6): a torn / missing table is image corruption that
   * recovery must surface as `journal_image_invalid` instead of silently
   * passing verification.
   */
  static #REQUIRED_JOURNAL_TABLES = [
    "journal_meta",
    "local_files",
    "journal_events",
    "journal_attempts",
    "lifecycle_event_operands",
    "device_sync_state",
    "manifest_page_progress",
    "manifest_action_progress",
    "remote_apply_operations",
    "echo_markers",
    "multipart_upload_progress",
    "conflict_local_repairs",
    "pending_rename_intents",
    "pending_rename_intent_missing_file_deferrals"
  ];
  /**
   * Read the manifest, distinguishing ABSENT from PRESENT-but-unverifiable:
   * a present manifest that fails to parse still proves journal artifacts
   * exist, which forces the rebuild path with `reconcile_required`. An
   * errored existence probe is neither: recovery fails closed instead of
   * reporting an absent store it could not actually observe.
   */
  async #readManifestState() {
    let isManifestPresent;
    try {
      isManifestPresent = await this.#fileStore.exists(JOURNAL_MANIFEST_FILE_NAME);
    } catch {
      throw journalStoreError("journal_store_unavailable");
    }
    if (!isManifestPresent) {
      return { isManifestPresent: false, manifest: null };
    }
    try {
      const bytes = new Uint8Array(await this.#fileStore.readBinary(JOURNAL_MANIFEST_FILE_NAME));
      return { isManifestPresent: true, manifest: parseJournalManifest(bytes) };
    } catch {
      return { isManifestPresent: true, manifest: null };
    }
  }
  async #recoverVerifiedDatabase(manifest) {
    const candidates = manifest === null ? [] : [
      { ...manifest.current, recoveryState: "verified_generation_loaded" },
      ...manifest.prior === null ? [] : [{ ...manifest.prior, recoveryState: "prior_generation_recovered" }]
    ];
    for (const candidate of candidates) {
      const opened = await this.#openVerifiedGeneration(candidate);
      if (opened !== null) {
        return {
          database: opened.database,
          verifiedGeneration: candidate,
          recoveryState: candidate.recoveryState,
          shouldPublishRecoveredImage: opened.shouldPublishRecoveredImage
        };
      }
    }
    return null;
  }
  /**
   * Read one candidate generation back, verify it byte-exactly and open
   * it. A generation whose manifest entry names a migration source
   * version (v6, v7 or v8) is migrated in memory first (task 8, task 9,
   * task 7): a v6 image walks the device-sync, multipart-progress and
   * conflict-repair migrations up to the current schema, a v7 image
   * takes the last two steps, a v8 image takes only the conflict-repair
   * step, and the migrated image is what recovery opens. Any migration
   * failure records the existing `startup_failure`/
   * `journal_recovery` trail surface before recovery falls back — never a
   * silent partial upgrade.
   */
  async #openVerifiedGeneration(candidate) {
    let migrationWasAttempted = false;
    try {
      const fileName = generationFileName(candidate.generationNumber);
      if (!await this.#fileStore.exists(fileName)) {
        return null;
      }
      const imageBytes = new Uint8Array(await this.#fileStore.readBinary(fileName));
      if (imageBytes.byteLength !== candidate.sizeBytes) {
        return null;
      }
      if (await sha256Hex(imageBytes) !== candidate.sha256) {
        return null;
      }
      let database;
      try {
        database = SqliteDatabase.openFromImage(this.#engineModule, imageBytes);
      } catch (error) {
        if (!(error instanceof JournalStoreError) || error.reason !== "journal_schema_unsupported" || !isKnownJournalMigrationSource(candidate.schemaVersion)) {
          throw error;
        }
        migrationWasAttempted = true;
        let migratedImage = imageBytes;
        if (candidate.schemaVersion === RESTORE_RESERVATION_SCHEMA_VERSION) {
          migratedImage = migrateRestoreReservationJournalToDeviceSyncSchema(
            this.#engineModule,
            migratedImage
          );
        }
        if (candidate.schemaVersion === RESTORE_RESERVATION_SCHEMA_VERSION || candidate.schemaVersion === DEVICE_SYNC_SCHEMA_VERSION) {
          migratedImage = migrateDeviceSyncJournalToMultipartProgressSchema(
            this.#engineModule,
            migratedImage
          );
        }
        if (candidate.schemaVersion !== CONFLICT_REPAIR_SCHEMA_VERSION) {
          migratedImage = migrateMultipartProgressJournalToConflictRepairSchema(
            this.#engineModule,
            migratedImage
          );
        }
        migratedImage = migrateConflictRepairJournalToPendingRenameIntentSchema(
          this.#engineModule,
          migratedImage
        );
        database = SqliteDatabase.openFromImage(this.#engineModule, migratedImage);
      }
      if (!_JournalPersistence.#databaseHasRequiredSurface(database)) {
        database.close();
        return null;
      }
      const pendingRenameStateWasRepaired = await _JournalPersistence.#repairInvalidPendingRenameState(database);
      return {
        database,
        shouldPublishRecoveredImage: migrationWasAttempted || pendingRenameStateWasRepaired
      };
    } catch (error) {
      if (migrationWasAttempted) {
        this.#recordSchemaMigrationFailure(error);
      }
      return null;
    }
  }
  /**
   * The production v6-to-v7 migration/recovery catch (task 8): one
   * fire-and-forget `startup_failure` entry carrying the closed
   * `journal_recovery` stage token plus the closed store reason, recorded
   * on the existing trail surface BEFORE recovery falls back to a prior
   * generation or rebuilds empty. Operation-specific `apply_failure` /
   * `reconcile_failure` entries belong to their own call sites (tasks
   * 10-11), never here.
   */
  #recordSchemaMigrationFailure(error) {
    const tokens = ["journal_recovery"];
    if (error instanceof JournalStoreError) {
      tokens.push(error.reason);
    }
    void this.#diagnosticTrail?.append({ kind: "startup_failure", tokens });
  }
  /**
   * Verify the required journal surface is intact on a freshly-opened
   * verified generation: every required table — including
   * `lifecycle_event_operands` and the five device-sync tables of
   * spec 8 — must be present. A missing table means the generation is
   * corrupt and recovery must fall back instead of trusting it.
   */
  static #databaseHasRequiredSurface(database) {
    try {
      const tables = database.readAll(
        "select name from sqlite_master where type = 'table' order by name;"
      );
      const present = new Set(
        (tables[0]?.values ?? []).map((row) => String(row[0]))
      );
      for (const required of _JournalPersistence.#REQUIRED_JOURNAL_TABLES) {
        if (!present.has(required)) {
          return false;
        }
      }
      return true;
    } catch {
      return false;
    }
  }
  static async #repairInvalidPendingRenameState(database) {
    return await database.runSerializedMutation((session) => {
      let didRepair = false;
      const markReconcileRequired = () => {
        didRepair = true;
        const meta = session.readJournalMeta();
        if (!meta.isReconcileRequired) {
          session.writeJournalMeta({ ...meta, isReconcileRequired: true });
        }
      };
      const clearIntentOnly = (localFileId) => {
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText5(localFileId)};`
        );
        session.exec(
          `delete from pending_rename_intents where local_file_id = ${sqlText5(localFileId)};`
        );
        markReconcileRequired();
      };
      const reconcileOwner = (localFileId, currentPath) => {
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText5(localFileId)};`
        );
        session.exec(
          `delete from pending_rename_intents where local_file_id = ${sqlText5(localFileId)};`
        );
        const occupant = _JournalPersistence.#firstRow(
          session,
          [
            "select local_file_id from local_files",
            `where normalized_path = ${sqlText5(currentPath)}`,
            `and local_file_id <> ${sqlText5(localFileId)} limit 1;`
          ].join(" ")
        );
        const pathWrite = occupant === null && _JournalPersistence.#isPendingRenamePath(currentPath) ? `normalized_path = ${sqlText5(currentPath)},` : "";
        session.exec(
          [
            "update local_files set",
            pathWrite,
            "lifecycle_state = 'reconcile_required',",
            "open_tombstone_id = null",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        markReconcileRequired();
      };
      const reconcileOwnerWithoutIntent = (localFileId) => {
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText5(localFileId)};`
        );
        session.exec(
          [
            "update local_files set",
            "lifecycle_state = 'reconcile_required',",
            "open_tombstone_id = null",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        markReconcileRequired();
      };
      const reconcileInvalidPendingRenameDeferral = (localFileId) => {
        const intent = _JournalPersistence.#firstRow(
          session,
          [
            "select current_path from pending_rename_intents",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        const owner = _JournalPersistence.#firstRow(
          session,
          [
            "select local_file_id from local_files",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        if (owner !== null && intent !== null && _JournalPersistence.#isPendingRenamePath(intent[0])) {
          reconcileOwner(localFileId, intent[0]);
          return;
        }
        if (intent !== null) {
          clearIntentOnly(localFileId);
        }
        if (owner !== null) {
          reconcileOwnerWithoutIntent(localFileId);
          return;
        }
        session.exec(
          `delete from pending_rename_intent_missing_file_deferrals where local_file_id = ${sqlText5(localFileId)};`
        );
        markReconcileRequired();
      };
      const intentRows = _JournalPersistence.#rows(
        session,
        "select local_file_id, prior_path, current_path from pending_rename_intents order by rowid asc;"
      );
      for (const row of intentRows) {
        const [localFileId, priorPath, currentPath] = row;
        if (typeof localFileId !== "string" || !_JournalPersistence.#isPendingRenamePath(priorPath) || !_JournalPersistence.#isPendingRenamePath(currentPath)) {
          if (typeof localFileId === "string") {
            clearIntentOnly(localFileId);
          } else {
            markReconcileRequired();
          }
          continue;
        }
        const owner = _JournalPersistence.#firstRow(
          session,
          [
            "select normalized_path, lifecycle_state from local_files",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        if (owner === null) {
          clearIntentOnly(localFileId);
          continue;
        }
        const [ownerPath, lifecycleState] = owner;
        if (typeof ownerPath !== "string" || typeof lifecycleState !== "string" || !_JournalPersistence.#isPendingRenameIntentRestartValid(session, {
          localFileId,
          priorPath,
          currentPath,
          ownerPath,
          lifecycleState
        })) {
          reconcileOwner(localFileId, currentPath);
        }
      }
      const deferralRows = _JournalPersistence.#rows(
        session,
        [
          "select local_file_id, event_id, deferred_attempt_count",
          "from pending_rename_intent_missing_file_deferrals order by rowid asc;"
        ].join(" ")
      );
      for (const row of deferralRows) {
        const [localFileId, eventId, deferredAttemptCount] = row;
        if (typeof localFileId !== "string" || typeof eventId !== "string" || typeof deferredAttemptCount !== "number" || !Number.isInteger(deferredAttemptCount) || deferredAttemptCount < 1 || deferredAttemptCount > 40) {
          if (typeof localFileId === "string") {
            reconcileInvalidPendingRenameDeferral(localFileId);
          } else {
            markReconcileRequired();
          }
          continue;
        }
        const intent = _JournalPersistence.#firstRow(
          session,
          [
            "select current_path from pending_rename_intents",
            `where local_file_id = ${sqlText5(localFileId)};`
          ].join(" ")
        );
        if (intent === null || !_JournalPersistence.#isPendingRenamePath(intent[0])) {
          reconcileInvalidPendingRenameDeferral(localFileId);
          continue;
        }
        if (!_JournalPersistence.#isPendingRenameDeferralRestartValid(session, localFileId, eventId)) {
          reconcileInvalidPendingRenameDeferral(localFileId);
        }
      }
      return didRepair;
    });
  }
  static #isPendingRenamePath(value) {
    return typeof value === "string" && value.length > 0 && !value.startsWith("/") && !value.includes("\\") && !value.split("/").some((segment) => segment.length === 0 || segment === "." || segment === "..");
  }
  static #isPendingRenameIntentRestartValid(session, intent) {
    if (intent.lifecycleState === "restore_pending" || intent.lifecycleState === "reconcile_required") {
      return false;
    }
    const occupant = _JournalPersistence.#firstRow(
      session,
      [
        "select local_file_id from local_files",
        `where normalized_path = ${sqlText5(intent.currentPath)}`,
        `and local_file_id <> ${sqlText5(intent.localFileId)} limit 1;`
      ].join(" ")
    );
    if (occupant !== null) {
      return false;
    }
    const prefixes = _JournalPersistence.#rows(
      session,
      [
        "select je.operation, leo.expected_locator, leo.target_locator",
        "from journal_events je",
        "join lifecycle_event_operands leo on leo.event_id = je.event_id",
        `where je.local_file_id = ${sqlText5(intent.localFileId)}`,
        "and je.operation in ('rename', 'move')",
        `and je.state in (${PENDING_RENAME_OPEN_EVENT_STATES_SQL})`,
        "order by je.created_at_epoch_ms asc, je.event_id asc;"
      ].join(" ")
    );
    if (prefixes.length === 0) {
      return intent.priorPath !== intent.currentPath && intent.ownerPath === intent.priorPath && intent.lifecycleState === PENDING_RENAME_UNMATERIALIZED_OWNER_STATE;
    }
    if (prefixes.length !== 1) {
      return false;
    }
    const [operation, expectedLocator, targetLocator] = prefixes[0] ?? [];
    return (operation === "rename" || operation === "move") && expectedLocator === intent.priorPath && _JournalPersistence.#isPendingRenamePath(targetLocator) && intent.ownerPath === targetLocator && intent.lifecycleState === `${operation}_pending`;
  }
  static #isPendingRenameDeferralRestartValid(session, localFileId, eventId) {
    const event = _JournalPersistence.#firstRow(
      session,
      [
        "select local_file_id, operation, state, safe_error from journal_events",
        `where event_id = ${sqlText5(eventId)};`
      ].join(" ")
    );
    if (event === null) {
      return false;
    }
    const [eventLocalFileId, operation, state, safeError] = event;
    return eventLocalFileId === localFileId && (operation === "create" || operation === "update") && state === "waiting_retry" && safeError === "deferred_lifecycle";
  }
  static #rows(session, sql) {
    return session.readRows(sql)[0]?.values ?? [];
  }
  static #firstRow(session, sql) {
    return _JournalPersistence.#rows(session, sql)[0] ?? null;
  }
  /** Record the recovery outcome in the working copy without re-publishing. */
  async #refreshRecoveredMeta(database, verifiedGeneration, recoveryState) {
    const meta = database.readJournalMeta();
    if (meta.lastVerifiedGeneration === verifiedGeneration.generationNumber && meta.recoveryState === recoveryState && meta.isReconcileRequired === this.#isReconcileRequired) {
      return;
    }
    await database.runSerializedMutation((session) => {
      session.writeJournalMeta({
        ...session.readJournalMeta(),
        lastVerifiedGeneration: verifiedGeneration.generationNumber,
        recoveryState,
        isReconcileRequired: this.#isReconcileRequired
      });
    });
  }
  async #rebuildEmptyJournal(isManifestPresent) {
    const hasJournalArtifacts = isManifestPresent || await this.#hasAnyGenerationFile();
    const vaultContentProbe = this.#hasVaultContent;
    const hasVaultContent = vaultContentProbe === null ? false : await this.#probeVaultContent(vaultContentProbe);
    const reconcileRequired = hasJournalArtifacts || hasVaultContent;
    if (reconcileRequired) {
      this.#isReconcileRequired = true;
    }
    const recoveryState = hasJournalArtifacts ? "empty_journal_rebuilt" : hasVaultContent ? "fresh_journal_reconcile_required" : "fresh_journal_created";
    const database = SqliteDatabase.createEmpty(this.#engineModule, {
      schemaVersion: JOURNAL_SCHEMA_VERSION,
      dirtyGeneration: 0,
      lastVerifiedGeneration: 0,
      isReconcileRequired: this.#isReconcileRequired,
      recoveryState
    });
    this.#database = database;
    this.#recoveryState = recoveryState;
    await this.#executeGenerationCommit(() => void 0);
  }
  async #hasAnyGenerationFile() {
    try {
      const names = await this.#fileStore.list();
      return names.some(isGenerationFileName);
    } catch {
      throw journalStoreError("journal_store_unavailable");
    }
  }
  async #probeVaultContent(probe) {
    try {
      return await probe();
    } catch {
      throw journalStoreError("journal_store_unavailable");
    }
  }
  async #executeGenerationCommit(operation) {
    const database = this.#requireOpenedDatabase();
    const nextGenerationNumber = (this.#verifiedGeneration?.generationNumber ?? 0) + 1;
    const result = await database.runSerializedMutation(async (session) => {
      const operationResult = await operation(session);
      const sessionMeta = session.readJournalMeta();
      this.#mergeReconcileRequired(sessionMeta.isReconcileRequired);
      session.writeJournalMeta({
        ...sessionMeta,
        dirtyGeneration: nextGenerationNumber,
        isReconcileRequired: this.#isReconcileRequired
      });
      return operationResult;
    });
    try {
      await this.#publishGeneration(nextGenerationNumber);
    } catch (error) {
      this.#recordGenerationPublishFailure(error);
      throw error;
    }
    return result;
  }
  /**
   * The closed-token view of generation publish failures (fix round 5):
   * the total count plus the last bounded reason tokens, newest last.
   * In-memory only; closed vocabulary only.
   */
  readGenerationPublishFailureSummary() {
    return {
      count: this.#generationPublishFailureCount,
      lastReasons: [...this.#generationPublishFailureReasons]
    };
  }
  /** Record one publish failure's closed reason, if it has one. */
  #recordGenerationPublishFailure(error) {
    if (!(error instanceof JournalStoreError)) {
      return;
    }
    this.#generationPublishFailureCount += 1;
    this.#generationPublishFailureReasons.push(error.reason);
    if (this.#generationPublishFailureReasons.length > MAX_GENERATION_PUBLISH_FAILURE_REASONS) {
      this.#generationPublishFailureReasons.shift();
    }
    void this.#diagnosticTrail?.append({ kind: "publish_failure", tokens: [error.reason] });
  }
  /**
   * The generation protocol of spec 6.2: write the image, read it back and
   * verify size/digest, publish the manifest, verify it, and only then
   * switch the verified chain and retire the older generation file.
   */
  async #publishGeneration(generationNumber) {
    const database = this.#requireOpenedDatabase();
    const image = database.exportImage();
    const sizeBytes = image.byteLength;
    const sha256 = await sha256Hex(image);
    const fileName = generationFileName(generationNumber);
    try {
      await this.#fileStore.writeBinary(fileName, toArrayBuffer2(image));
      const readBack = new Uint8Array(await this.#fileStore.readBinary(fileName));
      if (readBack.byteLength !== sizeBytes || await sha256Hex(readBack) !== sha256) {
        throw journalStoreError("journal_generation_write_failed");
      }
    } catch (error) {
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_generation_write_failed");
    }
    const manifest = {
      contract: JOURNAL_MANIFEST_CONTRACT,
      current: { generationNumber, sizeBytes, sha256, schemaVersion: JOURNAL_SCHEMA_VERSION },
      prior: this.#verifiedGeneration
    };
    await this.#writeVerifiedManifest(manifest);
    const retiredGeneration = this.#priorVerifiedGeneration;
    this.#priorVerifiedGeneration = this.#verifiedGeneration;
    this.#verifiedGeneration = manifest.current;
    await database.runSerializedMutation((session) => {
      const meta = session.readJournalMeta();
      this.#mergeReconcileRequired(meta.isReconcileRequired);
      session.writeJournalMeta({
        ...meta,
        dirtyGeneration: generationNumber,
        lastVerifiedGeneration: generationNumber,
        isReconcileRequired: this.#isReconcileRequired
      });
    });
    if (retiredGeneration !== null) {
      try {
        await this.#fileStore.remove(generationFileName(retiredGeneration.generationNumber));
      } catch {
      }
    }
  }
  async #writeVerifiedManifest(manifest) {
    const manifestBytes = new TextEncoder().encode(JSON.stringify(manifest));
    try {
      await this.#fileStore.writeBinary(
        JOURNAL_MANIFEST_FILE_NAME,
        toArrayBuffer2(manifestBytes)
      );
      const readBackBytes = new Uint8Array(
        await this.#fileStore.readBinary(JOURNAL_MANIFEST_FILE_NAME)
      );
      const readBack = parseJournalManifest(readBackBytes);
      if (readBack === null || !isSameJournalManifest(readBack, manifest)) {
        throw journalStoreError("journal_manifest_invalid");
      }
    } catch (error) {
      throw error instanceof JournalStoreError ? error : journalStoreError("journal_manifest_invalid");
    }
  }
};

// src/restore-modals.ts
var import_obsidian3 = require("obsidian");
var SuggestModal = class extends import_obsidian3.Modal {
  #items;
  #render;
  #placeholder = "Search\u2026";
  onChooseItem = () => void 0;
  constructor(app, items, render) {
    super(app);
    this.#items = items;
    this.#render = render;
  }
  setPlaceholder(text) {
    this.#placeholder = text;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("p", { text: this.#placeholder });
    const list = contentEl.createEl("ul");
    for (const item of this.#items) {
      const row = list.createEl("li", { text: this.#render(item) });
      row.style.cursor = "pointer";
      row.addEventListener("click", () => {
        this.onChooseItem(item);
        this.close();
      });
    }
  }
};
var TextPromptModal = class extends import_obsidian3.Modal {
  #title;
  #description;
  #accept;
  #reject;
  #inputValue = "";
  constructor(app, title, description, accept, reject) {
    super(app);
    this.#title = title;
    this.#description = description;
    this.#accept = accept;
    this.#reject = reject;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#description });
    const input = contentEl.createEl("input");
    input.type = "text";
    input.style.width = "100%";
    input.addEventListener("input", () => {
      this.#inputValue = input.value;
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.#accept(this.#inputValue);
        this.close();
      }
    });
    new import_obsidian3.Setting(contentEl).addButton(
      (button) => button.setButtonText("Restore").setCta().onClick(() => {
        this.#accept(this.#inputValue);
        this.close();
      })
    ).addButton(
      (button) => button.setButtonText("Cancel").onClick(() => {
        this.close();
        this.#reject();
      })
    );
    this.onClose = () => this.#reject();
  }
};
var ConfirmModal = class extends import_obsidian3.Modal {
  #title;
  #body;
  #accept;
  #reject;
  #dismiss;
  #hasResolved = false;
  constructor(app, title, body, accept, reject, dismiss) {
    super(app);
    this.#title = title;
    this.#body = body;
    this.#accept = accept;
    this.#reject = reject;
    this.#dismiss = dismiss;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#body });
    new import_obsidian3.Setting(contentEl).addButton(
      (button) => button.setButtonText("Restore").setCta().onClick(() => {
        this.#resolveWith(this.#accept);
        this.close();
      })
    ).addButton(
      (button) => button.setButtonText("Cancel").onClick(() => {
        this.#resolveWith(this.#reject);
        this.close();
      })
    );
    this.onClose = () => this.#resolveWith(this.#dismiss);
  }
  #resolveWith(callback) {
    if (this.#hasResolved) {
      return;
    }
    this.#hasResolved = true;
    callback();
  }
};
var PreformattedTextModal = class extends import_obsidian3.Modal {
  #title;
  #body;
  constructor(app, title, body) {
    super(app);
    this.#title = title;
    this.#body = body;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("pre", { text: this.#body });
    new import_obsidian3.Setting(contentEl).addButton(
      (button) => button.setButtonText("Close").setCta().onClick(() => this.close())
    );
  }
};

// src/journal/status.ts
var SYNC_STATUS_TEXT = {
  ready: "Ready",
  syncing: "Syncing",
  offline_queued: "Offline \u2014 queued",
  login_required: "Login required",
  policy_blocked: "Policy blocked",
  reconcile_required: "Reconcile required"
};
var LIFECYCLE_BLOCKED_REASON_CODES = [
  "idempotency_conflict",
  "version_conflict",
  "locator_conflict",
  "tombstone_not_found",
  "tombstone_closed",
  "commit_outcome_unknown",
  "integrity_failed"
];
var SYNC_BLOCKER_GUIDANCE_TEXT = {
  blocked_size: "This file is larger than the 16 MiB small-file limit and was not uploaded. Larger files arrive later through multipart upload.",
  excluded_policy: "This content is blocked by the sync policy. The policy is refreshed only through the authorized login flow, never from the sync status.",
  blocked_conflict: "No overwrite occurred: this change conflicts with the server version and resolution is owned by the later conflict flow.",
  deferred_lifecycle: "No overwrite occurred: rename/move/delete changes are owned by the later lifecycle flow.",
  login_required: "Login required: open the existing browser login from the plugin settings. Queued work is kept unchanged.",
  reconcile_required: 'Sync stopped: journal reconciliation is required before syncing can continue. Run the plugin command "Repair sync" to reconcile this device; queued work resumes after the repair completes.'
};
var WAITING_RETRY_SAFE_ERRORS = /* @__PURE__ */ new Set([
  "network_offline",
  "network_timeout",
  "network_rate_limited",
  "server_error",
  "login_required"
]);
var ZERO_LIFECYCLE_STATE_COUNTS = LIFECYCLE_LOCAL_FILE_STATES.reduce(
  (acc, state) => {
    acc[state] = 0;
    return acc;
  },
  {}
);
var ZERO_MULTIPART_STATE_COUNTS = MULTIPART_SESSION_STATES.reduce(
  (acc, state) => {
    acc[state] = 0;
    return acc;
  },
  {}
);
function projectJournalSyncStatus(input) {
  let pendingEventCount = 0;
  let hasWaitingRetryPending = false;
  let hasPolicyBlockedEvents = false;
  for (const row of input.eventStateErrorCounts) {
    if (JOURNAL_PENDING_EVENT_STATES.includes(row.state)) {
      pendingEventCount += row.eventCount;
    }
    if (row.state === "waiting_retry" && row.safeError !== null) {
      hasWaitingRetryPending ||= WAITING_RETRY_SAFE_ERRORS.has(row.safeError);
    }
    hasPolicyBlockedEvents ||= row.state === "excluded_policy" && row.eventCount > 0;
  }
  const blockers = [];
  if (input.isReconcileRequired) {
    blockers.push("reconcile_required");
  }
  const isLoginRequired = !input.hasAccessCredential && (pendingEventCount > 0 || input.lastQueuePassOutcome === "login_required");
  if (isLoginRequired) {
    blockers.push("login_required");
  }
  for (const [blocker, state] of [
    ["blocked_size", "blocked_size"],
    ["excluded_policy", "excluded_policy"],
    ["blocked_conflict", "blocked_conflict"],
    ["deferred_lifecycle", "deferred_lifecycle"]
  ]) {
    if (input.eventStateErrorCounts.some((row) => row.state === state && row.eventCount > 0)) {
      blockers.push(blocker);
    }
  }
  const kind = input.isReconcileRequired ? "reconcile_required" : isLoginRequired ? "login_required" : input.isQueuePassActive ? "syncing" : hasWaitingRetryPending ? "offline_queued" : hasPolicyBlockedEvents ? "policy_blocked" : "ready";
  const lifecycleStateCounts = normaliseLifecycleStateCounts(input.lifecycleStateCounts);
  const pendingLifecycleEventCount = normaliseNonNegativeCount(input.pendingLifecycleEventCount);
  const failedAttemptCount = normaliseNonNegativeCount(input.failedAttemptCount);
  const lifecycleBlockedReasonCodes = normaliseLifecycleBlockedReasonCodes(
    input.lifecycleBlockedReasonCodes
  );
  const multipartSessionStateCounts = normaliseMultipartSessionStateCounts(
    input.multipartSessionStateCounts
  );
  const multipartSafeReasonCodes = normaliseMultipartSafeReasonCodes(
    input.multipartSafeReasonCodes
  );
  const conflictApplyPendingCount = normaliseNonNegativeCount(input.conflictApplyPendingCount);
  const conflictApplySafeReasonTokens = normaliseConflictApplySafeReasonTokens(
    input.conflictApplySafeReasonTokens
  );
  return {
    kind,
    pendingEventCount,
    blockers,
    lifecycleStateCounts,
    pendingLifecycleEventCount,
    failedAttemptCount,
    lifecycleBlockedReasonCodes,
    multipartSessionStateCounts,
    multipartSafeReasonCodes,
    conflictApplyPendingCount,
    conflictApplySafeReasonTokens
  };
}
function normaliseLifecycleStateCounts(value) {
  if (value === void 0) {
    return ZERO_LIFECYCLE_STATE_COUNTS;
  }
  const counts = { ...ZERO_LIFECYCLE_STATE_COUNTS };
  for (const state of LIFECYCLE_LOCAL_FILE_STATES) {
    const candidate = value[state];
    if (candidate === void 0) {
      continue;
    }
    if (!Number.isInteger(candidate) || candidate < 0) {
      return ZERO_LIFECYCLE_STATE_COUNTS;
    }
    counts[state] = candidate;
  }
  return counts;
}
function normaliseNonNegativeCount(value) {
  if (value === void 0) {
    return 0;
  }
  if (!Number.isInteger(value) || value < 0) {
    return 0;
  }
  return value;
}
function normaliseLifecycleBlockedReasonCodes(value) {
  if (value === void 0) {
    return [];
  }
  const seen = /* @__PURE__ */ new Set();
  const filtered = [];
  for (const candidate of value) {
    if (typeof candidate === "string" && LIFECYCLE_BLOCKED_REASON_CODES.includes(candidate) && !seen.has(candidate)) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}
function normaliseMultipartSessionStateCounts(value) {
  if (value === void 0) {
    return ZERO_MULTIPART_STATE_COUNTS;
  }
  const counts = { ...ZERO_MULTIPART_STATE_COUNTS };
  for (const state of MULTIPART_SESSION_STATES) {
    const candidate = value[state];
    if (candidate === void 0) {
      continue;
    }
    if (!Number.isInteger(candidate) || candidate < 0) {
      return ZERO_MULTIPART_STATE_COUNTS;
    }
    counts[state] = candidate;
  }
  return counts;
}
function normaliseMultipartSafeReasonCodes(value) {
  if (value === void 0) {
    return [];
  }
  const seen = /* @__PURE__ */ new Set();
  const filtered = [];
  for (const candidate of value) {
    if (typeof candidate === "string" && MULTIPART_SAFE_REASON_TOKENS.includes(candidate) && !seen.has(candidate)) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}
function normaliseConflictApplySafeReasonTokens(value) {
  if (value === void 0) {
    return [];
  }
  const seen = /* @__PURE__ */ new Set();
  const filtered = [];
  for (const candidate of value) {
    if (typeof candidate === "string" && CONFLICT_LOCAL_REPAIR_SAFE_REASONS.includes(candidate) && !seen.has(candidate)) {
      seen.add(candidate);
      filtered.push(candidate);
    }
  }
  return filtered;
}
function renderJournalSyncStatusText(snapshot) {
  const countSuffix = snapshot.pendingEventCount > 0 ? ` (${snapshot.pendingEventCount})` : "";
  return `${SYNC_STATUS_TEXT[snapshot.kind]}${countSuffix}`;
}
function renderJournalSyncStatus(snapshot) {
  const baseText = renderJournalSyncStatusText(snapshot);
  if (snapshot.conflictApplyPendingCount <= 0) {
    return baseText;
  }
  return `${baseText} \xB7 Conflict apply pending (${snapshot.conflictApplyPendingCount})`;
}
function syncBlockerGuidanceLines(snapshot) {
  return snapshot.blockers.map((blocker) => SYNC_BLOCKER_GUIDANCE_TEXT[blocker]);
}

// src/journal/diagnostic-reporter.ts
function createJournalFailureReporter(trail) {
  return {
    reportJournalFailure(token) {
      void trail?.append({ kind: "journal_failure", tokens: [token] });
    }
  };
}

// src/journal/sync-self-check.ts
var SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS = 5e3;
function countTrailProbeEntries(entries) {
  return entries.filter(
    (entry) => entry.kind === "self_check" && entry.tokens.includes("trail_probe")
  ).length;
}
async function runTrailPersistProbeStep(trail) {
  const appendFailureCountBefore = trail.readAppendFailureCount();
  const probeEntriesBefore = countTrailProbeEntries(trail.readEntries());
  await trail.append({ kind: "self_check", tokens: ["trail_probe"] });
  const isPersisted = countTrailProbeEntries(trail.readEntries()) === probeEntriesBefore + 1 && trail.readAppendFailureCount() === appendFailureCountBefore;
  const verdict = isPersisted ? "trail_persist_ok" : "trail_persist_failed";
  await trail.append({ kind: "self_check", tokens: [verdict] });
  return { step: "trail_persist", verdict, networkKind: null };
}
async function runCredentialPresenceStep(trail, hasAccessCredential) {
  const verdict = hasAccessCredential() ? "credential_present" : "credential_absent";
  await trail.append({ kind: "self_check", tokens: [verdict] });
  return { step: "credential_presence", verdict, networkKind: null };
}
function probeOriginUnderTimeout(probeOrigin, timeoutMs) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("network_timeout"), timeoutMs);
    probeOrigin().then(
      () => {
        clearTimeout(timer);
        resolve(null);
      },
      () => {
        clearTimeout(timer);
        resolve("network_offline");
      }
    );
  });
}
async function runOriginReachabilityStep(trail, probeOrigin, timeoutMs) {
  const networkKind = await probeOriginUnderTimeout(probeOrigin, timeoutMs);
  const verdict = networkKind === null ? "origin_reachable" : "origin_unreachable";
  const tokens = networkKind === null ? [verdict] : [verdict, networkKind];
  await trail.append({ kind: "self_check", tokens });
  return { step: "origin_reachability", verdict, networkKind };
}
async function runSyncSelfCheck(options) {
  const originProbeTimeoutMs = options.originProbeTimeoutMs ?? SYNC_SELF_CHECK_ORIGIN_PROBE_TIMEOUT_MS;
  const steps = [
    await runTrailPersistProbeStep(options.trail),
    await runCredentialPresenceStep(options.trail, options.hasAccessCredential),
    await runOriginReachabilityStep(options.trail, options.probeOrigin, originProbeTimeoutMs)
  ];
  return { steps };
}
function renderSyncSelfCheckSummaryText(summary) {
  const stepTexts = summary.steps.map(
    (step) => step.networkKind === null ? step.verdict : `${step.verdict} \xB7 ${step.networkKind}`
  );
  return `Sync self-check: ${stepTexts.join(" \xB7 ")}`;
}
function renderSyncSelfCheckJournalNotRunningText(startupFailureTokens) {
  const baseLine = "Sync self-check unavailable: journal not running on this device.";
  if (startupFailureTokens === null || startupFailureTokens.length === 0) {
    return baseLine;
  }
  return `${baseLine} Journal startup failed: ${startupFailureTokens.join(", ")}`;
}

// src/journal/uuidv7.ts
var HEX_DIGITS = "0123456789abcdef";
function toHex(byte) {
  const high = HEX_DIGITS[byte >>> 4 & 15] ?? "0";
  const low = HEX_DIGITS[byte & 15] ?? "0";
  return high + low;
}
function randomBytes(length) {
  const buffer = new Uint8Array(length);
  crypto.getRandomValues(buffer);
  return buffer;
}
function createUuidv7Factory(options = {}) {
  const nowEpochMs = options.nowEpochMs ?? (() => Date.now());
  const random = options.randomBytes ?? randomBytes;
  let lastTimestampMs = -1;
  let counter = 0;
  return function nextUuidv7() {
    let timestampMs = nowEpochMs();
    if (timestampMs === lastTimestampMs) {
      counter = counter + 1 & 4095;
      if (counter === 0) {
        timestampMs += 1;
      }
    } else {
      lastTimestampMs = timestampMs;
      counter = 0;
    }
    const tailBytes = random(10);
    const byte0 = tailBytes[0] ?? 0;
    const byte1 = tailBytes[1] ?? 0;
    const byte2 = tailBytes[2] ?? 0;
    const byte3 = tailBytes[3] ?? 0;
    const byte4 = tailBytes[4] ?? 0;
    const byte5 = tailBytes[5] ?? 0;
    const byte6 = tailBytes[6] ?? 0;
    const byte7 = tailBytes[7] ?? 0;
    const bytes = new Uint8Array(16);
    bytes[0] = timestampMs / 1099511627776 & 255;
    bytes[1] = timestampMs / 4294967296 & 255;
    bytes[2] = timestampMs >>> 24 & 255;
    bytes[3] = timestampMs >>> 16 & 255;
    bytes[4] = timestampMs >>> 8 & 255;
    bytes[5] = timestampMs & 255;
    bytes[6] = 112 | counter >>> 8 & 15;
    bytes[7] = counter & 255;
    bytes[8] = 128 | byte0 & 63;
    bytes[9] = byte1;
    bytes[10] = byte2;
    bytes[11] = byte3;
    bytes[12] = byte4;
    bytes[13] = byte5;
    bytes[14] = byte6;
    bytes[15] = byte7;
    void tailBytes;
    let hex = "";
    for (let index = 0; index < 16; index += 1) {
      const byte = bytes[index] ?? 0;
      if (index === 4 || index === 6 || index === 8 || index === 10) {
        hex += "-";
      }
      hex += toHex(byte);
    }
    return hex;
  };
}

// src/exclusion-policy/strict-json.ts
var MAXIMUM_NESTING_DEPTH = 64;
function malformed() {
  throw policyVerificationError("policy_response_malformed");
}
function utf8ByteLength3(value) {
  let total = 0;
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit < 128) {
      total += 1;
    } else if (codeUnit < 2048) {
      total += 2;
    } else if (codeUnit >= 55296 && codeUnit <= 56319) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 56320 && next <= 57343) {
        total += 4;
        index += 1;
      } else {
        total += 3;
      }
    } else if (codeUnit >= 56320 && codeUnit <= 57343) {
      total += 3;
    } else {
      total += 3;
    }
  }
  return total;
}
function skipWhitespace(state) {
  while (state.position < state.text.length) {
    const character = state.text[state.position];
    if (character === " " || character === "	" || character === "\n" || character === "\r") {
      state.position += 1;
      continue;
    }
    return;
  }
}
function peek(state) {
  return state.text[state.position] ?? "";
}
function peekAt(state, offset) {
  return state.text[state.position + offset] ?? "";
}
function consume(state, expected) {
  if (state.text.startsWith(expected, state.position)) {
    state.position += expected.length;
    return;
  }
  malformed();
}
function parseValue(state, depth) {
  if (depth > MAXIMUM_NESTING_DEPTH) {
    malformed();
  }
  const character = peek(state);
  if (character === "{") {
    return parseObject(state, depth);
  }
  if (character === "[") {
    return parseArray(state, depth);
  }
  if (character === '"') {
    return parseString(state);
  }
  if (character === "t") {
    consume(state, "true");
    return true;
  }
  if (character === "f") {
    consume(state, "false");
    return false;
  }
  if (character === "n") {
    consume(state, "null");
    return null;
  }
  if (character === "-" || character >= "0" && character <= "9") {
    return parseInteger(state);
  }
  malformed();
}
function parseObject(state, depth) {
  consume(state, "{");
  const members = {};
  skipWhitespace(state);
  if (peek(state) === "}") {
    state.position += 1;
    return members;
  }
  for (; ; ) {
    skipWhitespace(state);
    if (peek(state) !== '"') {
      malformed();
    }
    const name = parseString(state);
    if (Object.prototype.hasOwnProperty.call(members, name)) {
      malformed();
    }
    skipWhitespace(state);
    if (peek(state) !== ":") {
      malformed();
    }
    state.position += 1;
    skipWhitespace(state);
    members[name] = parseValue(state, depth + 1);
    skipWhitespace(state);
    const separator = peek(state);
    if (separator === ",") {
      state.position += 1;
      continue;
    }
    if (separator === "}") {
      state.position += 1;
      return members;
    }
    malformed();
  }
}
function parseArray(state, depth) {
  consume(state, "[");
  const elements = [];
  skipWhitespace(state);
  if (peek(state) === "]") {
    state.position += 1;
    return elements;
  }
  for (; ; ) {
    skipWhitespace(state);
    elements.push(parseValue(state, depth + 1));
    skipWhitespace(state);
    const separator = peek(state);
    if (separator === ",") {
      state.position += 1;
      continue;
    }
    if (separator === "]") {
      state.position += 1;
      return elements;
    }
    malformed();
  }
}
var SHORT_ESCAPES = {
  '"': '"',
  "\\": "\\",
  "/": "/",
  b: "\b",
  f: "\f",
  n: "\n",
  r: "\r",
  t: "	"
};
function isHexDigit(character) {
  return character >= "0" && character <= "9" || character >= "a" && character <= "f" || character >= "A" && character <= "F";
}
function readHex4(state) {
  let value = 0;
  for (let digit = 0; digit < 4; digit += 1) {
    const character = peek(state);
    if (!isHexDigit(character)) {
      malformed();
    }
    value = value * 16 + Number.parseInt(character, 16);
    state.position += 1;
  }
  return value;
}
function parseString(state) {
  consume(state, '"');
  let pieces = "";
  for (; ; ) {
    if (state.position >= state.text.length) {
      malformed();
    }
    const character = state.text[state.position];
    if (character === '"') {
      state.position += 1;
      return pieces;
    }
    if (character === "\\") {
      state.position += 1;
      const escape = peek(state);
      if (escape === "u") {
        state.position += 1;
        const first = readHex4(state);
        if (first >= 55296 && first <= 56319) {
          if (peek(state) !== "\\" || peekAt(state, 1) !== "u") {
            malformed();
          }
          state.position += 2;
          const second = readHex4(state);
          if (second < 56320 || second > 57343) {
            malformed();
          }
          pieces += String.fromCharCode(first, second);
          continue;
        }
        if (first >= 56320 && first <= 57343) {
          malformed();
        }
        pieces += String.fromCharCode(first);
        continue;
      }
      const short = SHORT_ESCAPES[escape];
      if (short === void 0) {
        malformed();
      }
      pieces += short;
      state.position += 1;
      continue;
    }
    if (character === void 0) {
      malformed();
    }
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 32) {
      malformed();
    }
    if (codeUnit >= 55296 && codeUnit <= 57343) {
      const next = state.text.charCodeAt(state.position + 1);
      if (codeUnit <= 56319 && next >= 56320 && next <= 57343) {
        pieces += character + peekAt(state, 1);
        state.position += 2;
        continue;
      }
      malformed();
    }
    pieces += character;
    state.position += 1;
  }
}
var MAXIMUM_SAFE_INTEGER2 = 9007199254740991;
var MINIMUM_SAFE_INTEGER2 = -9007199254740991;
function parseInteger(state) {
  const start = state.position;
  if (peek(state) === "-") {
    state.position += 1;
  }
  const firstDigit = peek(state);
  if (!(firstDigit >= "0" && firstDigit <= "9")) {
    malformed();
  }
  if (firstDigit === "0") {
    state.position += 1;
  } else {
    while (peek(state) >= "0" && peek(state) <= "9") {
      state.position += 1;
    }
  }
  const next = peek(state);
  if (next === "." || next === "e" || next === "E" || next >= "0" && next <= "9") {
    malformed();
  }
  const value = Number(state.text.slice(start, state.position));
  if (!Number.isSafeInteger(value) || value > MAXIMUM_SAFE_INTEGER2 || value < MINIMUM_SAFE_INTEGER2) {
    malformed();
  }
  return value;
}
function parseClosedJson(text, options) {
  if (utf8ByteLength3(text) > options.maximumBytes) {
    throw policyVerificationError("policy_response_oversized");
  }
  const state = { text, position: 0 };
  skipWhitespace(state);
  const value = parseValue(state, 1);
  skipWhitespace(state);
  if (state.position !== state.text.length) {
    malformed();
  }
  return value;
}

// ../../node_modules/.pnpm/@noble+ed25519@3.1.0/node_modules/@noble/ed25519/index.js
var ed25519_CURVE = Object.freeze({
  p: 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffedn,
  n: 0x1000000000000000000000000000000014def9dea2f79cd65812631a5cf5d3edn,
  h: 8n,
  a: 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffecn,
  d: 0x52036cee2b6ffe738cc740797779e89800700a4d4141d8ab75eb4dca135978a3n,
  Gx: 0x216936d3cd6e53fec0a4e231fdd6dc5c692cc7609525a7b2c9562d608f25d51an,
  Gy: 0x6666666666666666666666666666666666666666666666666666666666666658n
});
var { p: P, n: N, Gx, Gy, a: _a, d: _d, h } = ed25519_CURVE;
var L = 32;
var captureTrace = (...args) => {
  if ("captureStackTrace" in Error && typeof Error.captureStackTrace === "function") {
    Error.captureStackTrace(...args);
  }
};
var err = (message = "") => {
  const e = new Error(message);
  captureTrace(e, err);
  throw e;
};
var isBig = (n) => typeof n === "bigint";
var isStr = (s) => typeof s === "string";
var isBytes = (a) => a instanceof Uint8Array || ArrayBuffer.isView(a) && a.constructor.name === "Uint8Array" && "BYTES_PER_ELEMENT" in a && a.BYTES_PER_ELEMENT === 1;
var abytes = (value, length, title = "") => {
  const bytes = isBytes(value);
  const len = value?.length;
  const needsLen = length !== void 0;
  if (!bytes || needsLen && len !== length) {
    const prefix = title && `"${title}" `;
    const ofLen = needsLen ? ` of length ${length}` : "";
    const got = bytes ? `length=${len}` : `type=${typeof value}`;
    const msg = prefix + "expected Uint8Array" + ofLen + ", got " + got;
    throw bytes ? new RangeError(msg) : new TypeError(msg);
  }
  return value;
};
var u8n = (len) => new Uint8Array(len);
var u8fr = (buf) => Uint8Array.from(buf);
var padh = (n, pad) => n.toString(16).padStart(pad, "0");
var bytesToHex = (b) => Array.from(abytes(b)).map((e) => padh(e, 2)).join("");
var C = { _0: 48, _9: 57, A: 65, F: 70, a: 97, f: 102 };
var _ch = (ch) => {
  if (ch >= C._0 && ch <= C._9)
    return ch - C._0;
  if (ch >= C.A && ch <= C.F)
    return ch - (C.A - 10);
  if (ch >= C.a && ch <= C.f)
    return ch - (C.a - 10);
  return;
};
var hexToBytes = (hex) => {
  const e = "hex invalid";
  if (!isStr(hex))
    return err(e);
  const hl = hex.length;
  const al = hl / 2;
  if (hl % 2)
    return err(e);
  const array = u8n(al);
  for (let ai = 0, hi = 0; ai < al; ai++, hi += 2) {
    const n1 = _ch(hex.charCodeAt(hi));
    const n2 = _ch(hex.charCodeAt(hi + 1));
    if (n1 === void 0 || n2 === void 0)
      return err(e);
    array[ai] = n1 * 16 + n2;
  }
  return array;
};
var cr = () => globalThis?.crypto;
var subtle = () => cr()?.subtle ?? err("crypto.subtle must be defined, consider polyfill");
var concatBytes = (...arrs) => {
  let len = 0;
  for (const a of arrs)
    len += abytes(a).length;
  const r = u8n(len);
  let pad = 0;
  arrs.forEach((a) => {
    r.set(a, pad);
    pad += a.length;
  });
  return r;
};
var big = BigInt;
var assertRange = (n, min, max, msg = "bad number: out of range") => {
  if (!isBig(n))
    throw new TypeError(msg);
  if (min <= n && n < max)
    return n;
  throw new RangeError(msg);
};
var M = (a, b = P) => {
  const r = a % b;
  return r >= 0n ? r : b + r;
};
var P_MASK = (1n << 255n) - 1n;
var modP = (num) => {
  if (num < 0n)
    err("negative coordinate");
  let r = (num >> 255n) * 19n + (num & P_MASK);
  r = (r >> 255n) * 19n + (r & P_MASK);
  return r % P;
};
var modN = (a) => M(a, N);
var invert = (num, md) => {
  if (num === 0n || md <= 0n)
    err("no inverse n=" + num + " mod=" + md);
  let a = M(num, md), b = md, x = 0n, y = 1n, u = 1n, v = 0n;
  while (a !== 0n) {
    const q = b / a, r = b % a;
    const m = x - u * q, n = y - v * q;
    b = a, a = r, x = u, y = v, u = m, v = n;
  }
  return b === 1n ? M(x, md) : err("no inverse");
};
var callHash = (name) => {
  const fn = hashes[name];
  if (typeof fn !== "function")
    err("hashes." + name + " not set");
  return fn;
};
var checkDigest = (value) => abytes(value, 64, "digest");
var apoint = (p) => p instanceof Point ? p : err("Point expected");
var B256 = 2n ** 256n;
var Point = class _Point {
  static BASE;
  static ZERO;
  X;
  Y;
  Z;
  T;
  // Constructor only bounds-checks and freezes XYZT coordinates; it does not prove the point is
  // on-curve or that T matches X*Y/Z.
  constructor(X, Y, Z, T) {
    const max = B256;
    this.X = assertRange(X, 0n, max);
    this.Y = assertRange(Y, 0n, max);
    this.Z = assertRange(Z, 1n, max);
    this.T = assertRange(T, 0n, max);
    Object.freeze(this);
  }
  static CURVE() {
    return ed25519_CURVE;
  }
  static fromAffine(p) {
    return new _Point(p.x, p.y, 1n, modP(p.x * p.y));
  }
  /** RFC8032 5.1.3: Bytes to Point. */
  static fromBytes(hex, zip215 = false) {
    const d = _d;
    const normed = u8fr(abytes(hex, L));
    const lastByte = hex[31];
    normed[31] = lastByte & ~128;
    const y = bytesToNumberLE(normed);
    const max = zip215 ? B256 : P;
    assertRange(y, 0n, max);
    const y2 = modP(y * y);
    const u = M(y2 - 1n);
    const v = modP(d * y2 + 1n);
    let { isValid, value: x } = uvRatio(u, v);
    if (!isValid)
      err("bad point: y not sqrt");
    const isXOdd = (x & 1n) === 1n;
    const isLastByteOdd = (lastByte & 128) !== 0;
    if (!zip215 && x === 0n && isLastByteOdd)
      err("bad point: x==0, isLastByteOdd");
    if (isLastByteOdd !== isXOdd)
      x = M(-x);
    return new _Point(x, y, 1n, modP(x * y));
  }
  static fromHex(hex, zip215) {
    return _Point.fromBytes(hexToBytes(hex), zip215);
  }
  get x() {
    return this.toAffine().x;
  }
  get y() {
    return this.toAffine().y;
  }
  /** Checks if the point is valid and on-curve. */
  assertValidity() {
    const a = _a;
    const d = _d;
    const p = this;
    if (p.is0())
      return err("bad point: ZERO");
    const { X, Y, Z, T } = p;
    const X2 = modP(X * X);
    const Y2 = modP(Y * Y);
    const Z2 = modP(Z * Z);
    const Z4 = modP(Z2 * Z2);
    const aX2 = modP(X2 * a);
    const left = modP(Z2 * (aX2 + Y2));
    const right = M(Z4 + modP(d * modP(X2 * Y2)));
    if (left !== right)
      return err("bad point: equation left != right (1)");
    const XY = modP(X * Y);
    const ZT = modP(Z * T);
    if (XY !== ZT)
      return err("bad point: equation left != right (2)");
    return this;
  }
  /** Equality check: compare points P&Q. */
  equals(other) {
    const { X: X1, Y: Y1, Z: Z1 } = this;
    const { X: X2, Y: Y2, Z: Z2 } = apoint(other);
    const X1Z2 = modP(X1 * Z2);
    const X2Z1 = modP(X2 * Z1);
    const Y1Z2 = modP(Y1 * Z2);
    const Y2Z1 = modP(Y2 * Z1);
    return X1Z2 === X2Z1 && Y1Z2 === Y2Z1;
  }
  is0() {
    return this.equals(I);
  }
  /** Flip point over y coordinate. */
  negate() {
    return new _Point(M(-this.X), this.Y, this.Z, M(-this.T));
  }
  /** Point doubling. Complete formula. Cost: `4M + 4S + 1*a + 6add + 1*2`. */
  double() {
    const { X: X1, Y: Y1, Z: Z1 } = this;
    const a = _a;
    const A = modP(X1 * X1);
    const B = modP(Y1 * Y1);
    const C2 = modP(2n * Z1 * Z1);
    const D = modP(a * A);
    const x1y1 = M(X1 + Y1);
    const E = M(modP(x1y1 * x1y1) - A - B);
    const G2 = M(D + B);
    const F = M(G2 - C2);
    const H = M(D - B);
    const X3 = modP(E * F);
    const Y3 = modP(G2 * H);
    const T3 = modP(E * H);
    const Z3 = modP(F * G2);
    return new _Point(X3, Y3, Z3, T3);
  }
  /** Point addition. Complete formula. Cost: `8M + 1*k + 8add + 1*2`. */
  add(other) {
    const { X: X1, Y: Y1, Z: Z1, T: T1 } = this;
    const { X: X2, Y: Y2, Z: Z2, T: T2 } = apoint(other);
    const a = _a;
    const d = _d;
    const A = modP(X1 * X2);
    const B = modP(Y1 * Y2);
    const C2 = modP(modP(T1 * d) * T2);
    const D = modP(Z1 * Z2);
    const E = M(modP(M(X1 + Y1) * M(X2 + Y2)) - A - B);
    const F = M(D - C2);
    const G2 = M(D + C2);
    const H = M(B - modP(a * A));
    const X3 = modP(E * F);
    const Y3 = modP(G2 * H);
    const T3 = modP(E * H);
    const Z3 = modP(F * G2);
    return new _Point(X3, Y3, Z3, T3);
  }
  subtract(other) {
    return this.add(apoint(other).negate());
  }
  /**
   * Point-by-scalar multiplication. Safe mode requires `1 <= n < CURVE.n`.
   * Unsafe mode additionally permits `n = 0` and returns the identity point for that case.
   * Uses {@link wNAF} for base point.
   * Uses fake point to mitigate side-channel leakage.
   * @param n - scalar by which point is multiplied
   * @param safe - safe mode guards against timing attacks; unsafe mode is faster
   */
  multiply(n, safe = true) {
    if (!safe && n === 0n)
      return I;
    assertRange(n, 1n, N);
    if (!safe && this.is0())
      return I;
    if (n === 1n)
      return this;
    if (this.equals(G))
      return wNAF(n).p;
    let p = I;
    let f = G;
    for (let d = this; n > 0n; d = d.double(), n >>= 1n) {
      if (n & 1n)
        p = p.add(d);
      else if (safe)
        f = f.add(d);
    }
    return p;
  }
  multiplyUnsafe(scalar) {
    return this.multiply(scalar, false);
  }
  /** Convert point to 2d xy affine point. (X, Y, Z) ∋ (x=X/Z, y=Y/Z) */
  toAffine() {
    const { X, Y, Z } = this;
    if (this.equals(I))
      return { x: 0n, y: 1n };
    const iz = invert(Z, P);
    if (modP(Z * iz) !== 1n)
      err("invalid inverse");
    const x = modP(X * iz);
    const y = modP(Y * iz);
    return { x, y };
  }
  toBytes() {
    const { x, y } = this.toAffine();
    const b = numTo32bLE(y);
    b[31] |= x & 1n ? 128 : 0;
    return b;
  }
  toHex() {
    return bytesToHex(this.toBytes());
  }
  clearCofactor() {
    return this.multiply(big(h), false);
  }
  isSmallOrder() {
    return this.clearCofactor().is0();
  }
  isTorsionFree() {
    let p = this.multiply(N / 2n, false).double();
    if (N % 2n)
      p = p.add(this);
    return p.is0();
  }
};
var G = new Point(Gx, Gy, 1n, M(Gx * Gy));
var I = new Point(0n, 1n, 1n, 0n);
Point.BASE = G;
Point.ZERO = I;
var numTo32bLE = (num) => hexToBytes(padh(assertRange(num, 0n, B256), 64)).reverse();
var bytesToNumberLE = (b) => big("0x" + bytesToHex(u8fr(abytes(b)).reverse()));
var pow2 = (x, power) => {
  let r = x;
  while (power-- > 0n) {
    r = modP(r * r);
  }
  return r;
};
var pow_2_252_3 = (x) => {
  const x2 = modP(x * x);
  const b2 = modP(x2 * x);
  const b4 = modP(pow2(b2, 2n) * b2);
  const b5 = modP(pow2(b4, 1n) * x);
  const b10 = modP(pow2(b5, 5n) * b5);
  const b20 = modP(pow2(b10, 10n) * b10);
  const b40 = modP(pow2(b20, 20n) * b20);
  const b80 = modP(pow2(b40, 40n) * b40);
  const b160 = modP(pow2(b80, 80n) * b80);
  const b240 = modP(pow2(b160, 80n) * b80);
  const b250 = modP(pow2(b240, 10n) * b10);
  const pow_p_5_8 = modP(pow2(b250, 2n) * x);
  return { pow_p_5_8, b2 };
};
var RM1 = 0x2b8324804fc1df0b2b4d00993dfbd7a72f431806ad2fe478c4ee1b274a0ea0b0n;
var uvRatio = (u, v) => {
  const v3 = modP(v * modP(v * v));
  const v7 = modP(modP(v3 * v3) * v);
  const pow = pow_2_252_3(modP(u * v7)).pow_p_5_8;
  let x = modP(u * modP(v3 * pow));
  const vx2 = modP(v * modP(x * x));
  const root1 = x;
  const root2 = modP(x * RM1);
  const useRoot1 = vx2 === u;
  const useRoot2 = vx2 === M(-u);
  const noRoot = vx2 === M(-u * RM1);
  if (useRoot1)
    x = root1;
  if (useRoot2 || noRoot)
    x = root2;
  if ((M(x) & 1n) === 1n)
    x = M(-x);
  return { isValid: useRoot1 || useRoot2, value: x };
};
var modL_LE = (hash) => modN(bytesToNumberLE(hash));
var sha512a = (...m) => Promise.resolve(callHash("sha512Async")(concatBytes(...m))).then(checkDigest);
var hashFinishA = (res) => sha512a(res.hashable).then(res.finish);
var defaultVerifyOpts = { zip215: true };
var _verify = (sig, msg, publicKey, options = defaultVerifyOpts) => {
  sig = abytes(sig, 64);
  msg = abytes(msg);
  publicKey = abytes(publicKey, L);
  const { zip215 = true } = options;
  const r = sig.subarray(0, L);
  const s = bytesToNumberLE(sig.subarray(L, L * 2));
  let A, R, SB;
  let hashable = Uint8Array.of();
  let finished = false;
  try {
    A = Point.fromBytes(publicKey, zip215);
    R = Point.fromBytes(r, zip215);
    SB = G.multiply(s, false);
    hashable = concatBytes(r, publicKey, msg);
    finished = true;
  } catch (error) {
  }
  const finish = (hashed) => {
    if (!finished)
      return false;
    if (!zip215 && A.isSmallOrder())
      return false;
    const k = modL_LE(hashed);
    const RkA = R.add(A.multiply(k, false));
    return RkA.subtract(SB).clearCofactor().is0();
  };
  return { hashable, finish };
};
var verifyAsync = async (signature, message, publicKey, opts = defaultVerifyOpts) => hashFinishA(_verify(signature, message, publicKey, opts));
var hashes = {
  sha512Async: async (message) => {
    const s = subtle();
    const m = concatBytes(message);
    return u8n(await s.digest("SHA-512", m.buffer));
  },
  sha512: void 0
};
var W = 8;
var scalarBits = 256;
var pwindows = Math.ceil(scalarBits / W) + 1;
var pwindowSize = 2 ** (W - 1);
var precompute = () => {
  const points = [];
  let p = G;
  let b = p;
  for (let w = 0; w < pwindows; w++) {
    b = p;
    points.push(b);
    for (let i = 1; i < pwindowSize; i++) {
      b = b.add(p);
      points.push(b);
    }
    p = b.double();
  }
  return points;
};
var Gpows = void 0;
var ctneg = (cnd, p) => {
  const n = p.negate();
  return cnd ? n : p;
};
var wNAF = (n) => {
  const comp = Gpows || (Gpows = precompute());
  let p = I;
  let f = G;
  const pow_2_w = 2 ** W;
  const maxNum = pow_2_w;
  const mask = big(pow_2_w - 1);
  const shiftBy = big(W);
  for (let w = 0; w < pwindows; w++) {
    let wbits = Number(n & mask);
    n >>= shiftBy;
    if (wbits > pwindowSize) {
      wbits -= maxNum;
      n += 1n;
    }
    const off = w * pwindowSize;
    const offF = off;
    const offP = off + Math.abs(wbits) - 1;
    const isEven = w % 2 !== 0;
    const isNeg = wbits < 0;
    if (wbits === 0) {
      f = f.add(ctneg(isEven, comp[offF]));
    } else {
      p = p.add(ctneg(isNeg, comp[offP]));
    }
  }
  if (n !== 0n)
    err("invalid wnaf");
  return { p, f };
};

// src/exclusion-policy/keyset.ts
var BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
function encodeBase64UrlWithoutPadding(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function decodeBase64UrlWithoutPadding(text) {
  if (text.length === 0 || text.length % 4 === 1 || !BASE64URL_PATTERN.test(text)) {
    return null;
  }
  const padded = text + "=".repeat((4 - text.length % 4) % 4);
  const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}
async function deriveEd25519KeyId(publicKey) {
  return KEY_ID_PREFIX + encodeBase64UrlWithoutPadding(
    new Uint8Array(await crypto.subtle.digest("SHA-256", publicKey))
  );
}
function isWellFormedEd25519KeyId(keyId) {
  return keyId.startsWith(KEY_ID_PREFIX) && keyId.length === KEY_ID_PREFIX.length + 43 && BASE64URL_PATTERN.test(keyId.slice(KEY_ID_PREFIX.length));
}
function buildSignedPolicyMessage(domain, payloadBytes) {
  const domainBytes = new TextEncoder().encode(domain);
  const message = new Uint8Array(domainBytes.length + 1 + payloadBytes.length);
  message.set(domainBytes, 0);
  message[domainBytes.length] = 0;
  message.set(payloadBytes, domainBytes.length + 1);
  return message;
}
var isSha512Configured = false;
function configureEd25519WebCryptoSha512() {
  if (isSha512Configured) {
    return;
  }
  hashes.sha512Async = async (message) => new Uint8Array(await crypto.subtle.digest("SHA-512", message));
  isSha512Configured = true;
}
async function verifyDetachedEd25519(message, signature, publicKey) {
  if (signature.length !== ED25519_SIGNATURE_BYTES || publicKey.length !== ED25519_PUBLIC_KEY_BYTES) {
    return false;
  }
  configureEd25519WebCryptoSha512();
  try {
    return await verifyAsync(signature, message, publicKey);
  } catch {
    return false;
  }
}
var UUID_PATTERN11 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
var HEX_SHA256_PATTERN = /^[0-9a-f]{64}$/;
var KEY_STATES = ["current", "staged", "retired"];
function asObject(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function requireExactMembers(value, expected) {
  for (const name of Object.keys(value)) {
    if (!expected.includes(name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  for (const name of expected) {
    if (!Object.prototype.hasOwnProperty.call(value, name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
}
function requireString(value) {
  if (typeof value !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function requireInteger(value, minimum) {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function validateSignatureMember(value) {
  const member = asObject(value);
  requireExactMembers(member, ["algorithm", "key_id", "value"]);
  if (member["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString(member["key_id"]);
  const signatureValue = requireString(member["value"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (signatureValue.length !== 86 || !BASE64URL_PATTERN.test(signatureValue)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return { keyId, value: signatureValue };
}
function validateKeyMember(value) {
  const member = asObject(value);
  requireExactMembers(member, ["algorithm", "key_id", "public_key", "state"]);
  if (member["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString(member["key_id"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const publicKey = requireString(member["public_key"]);
  if (publicKey.length !== 43 || !BASE64URL_PATTERN.test(publicKey)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const state = requireString(member["state"]);
  if (!KEY_STATES.includes(state)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    algorithm: SIGNATURE_ALGORITHM,
    key_id: keyId,
    public_key: publicKey,
    state
  };
}
function validateKeysetPayload(value) {
  const payload = asObject(value);
  requireExactMembers(payload, [
    "contract",
    "workspace_id",
    "keyset_revision",
    "parent_keyset_revision",
    "created_at",
    "keys"
  ]);
  if (payload["contract"] !== KEYSET_PAYLOAD_CONTRACT) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!UUID_PATTERN11.test(requireString(payload["workspace_id"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keysetRevision = requireInteger(payload["keyset_revision"], 1);
  const parent = payload["parent_keyset_revision"];
  if (parent !== null && (typeof parent !== "number" || !Number.isInteger(parent) || parent < 1)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (keysetRevision === 1 && parent !== null) {
    throw policyVerificationError("policy_keyset_revision_invalid");
  }
  if (keysetRevision > 1 && parent !== keysetRevision - 1) {
    throw policyVerificationError("policy_keyset_revision_invalid");
  }
  if (!TIMESTAMP_PATTERN.test(requireString(payload["created_at"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(payload["keys"]) || payload["keys"].length === 0) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keys = payload["keys"].map((key) => validateKeyMember(key));
  const seenKeyIds = /* @__PURE__ */ new Set();
  let currentCount = 0;
  let nonRetiredCount = 0;
  for (const key of keys) {
    if (seenKeyIds.has(key.key_id)) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
    seenKeyIds.add(key.key_id);
    if (key.state === "current") {
      currentCount += 1;
    }
    if (key.state !== "retired") {
      nonRetiredCount += 1;
    }
  }
  if (currentCount !== 1) {
    throw policyVerificationError("policy_keyset_current_invalid");
  }
  if (nonRetiredCount > KEYSET_MAXIMUM_NON_RETIRED_KEYS) {
    throw policyVerificationError("policy_keyset_key_invalid");
  }
  return {
    contract: KEYSET_PAYLOAD_CONTRACT,
    workspace_id: requireString(payload["workspace_id"]),
    keyset_revision: keysetRevision,
    parent_keyset_revision: parent,
    created_at: requireString(payload["created_at"]),
    keys
  };
}
function validateKeysetEnvelope(value) {
  const envelope = asObject(value);
  requireExactMembers(envelope, ["payload", "payload_sha256", "signatures"]);
  const payload = validateKeysetPayload(envelope["payload"]);
  const payloadSha256 = requireString(envelope["payload_sha256"]);
  if (!HEX_SHA256_PATTERN.test(payloadSha256)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(envelope["signatures"]) || envelope["signatures"].length === 0) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const signatures = envelope["signatures"].map((signature) => {
    const validated = validateSignatureMember(signature);
    return {
      algorithm: SIGNATURE_ALGORITHM,
      key_id: validated.keyId,
      value: validated.value
    };
  });
  return { payload, payload_sha256: payloadSha256, signatures };
}
async function verifyKeysetEnvelopeIntegrity(envelope) {
  const validated = validateKeysetEnvelope(envelope);
  for (const key of validated.payload.keys) {
    const publicKey = decodeBase64UrlWithoutPadding(key.public_key);
    if (publicKey === null || publicKey.length !== ED25519_PUBLIC_KEY_BYTES) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
    if (await deriveEd25519KeyId(publicKey) !== key.key_id) {
      throw policyVerificationError("policy_keyset_key_invalid");
    }
  }
  const payloadBytes = canonicalJsonBytes(validated.payload);
  const payloadSha256 = await sha256Hex(payloadBytes);
  if (payloadSha256 !== validated.payload_sha256) {
    throw policyVerificationError("policy_payload_hash_mismatch");
  }
  return { payloadBytes, payloadSha256 };
}
async function verifyKeysetEnvelope(envelope) {
  const integrity = await verifyKeysetEnvelopeIntegrity(envelope);
  return {
    envelope: validateKeysetEnvelope(envelope),
    payloadBytes: integrity.payloadBytes,
    payloadSha256: integrity.payloadSha256
  };
}
async function verifyAnySignature(verified, keyId, signatureValue) {
  const signature = decodeBase64UrlWithoutPadding(signatureValue);
  if (signature === null) {
    return false;
  }
  const message = buildSignedPolicyMessage(KEYSET_SIGNING_DOMAIN, verified.payloadBytes);
  const keyEntry = verified.envelope.payload.keys.find((key) => key.key_id === keyId);
  if (keyEntry === void 0) {
    return false;
  }
  const publicKey = decodeBase64UrlWithoutPadding(keyEntry.public_key);
  if (publicKey === null) {
    return false;
  }
  return verifyDetachedEd25519(message, signature, publicKey);
}
function keyStateOf(envelope, keyId) {
  return envelope.payload.keys.find((key) => key.key_id === keyId)?.state ?? null;
}
async function verifyKeysetChain(input) {
  const firstEnvelope = input.envelopes[0];
  if (firstEnvelope === void 0) {
    throw policyVerificationError("policy_keyset_chain_gap");
  }
  if (input.trustedKeyset === null) {
    if (!input.allowInitialTrust) {
      throw policyVerificationError("policy_onboarding_boundary_violation");
    }
    const first = firstEnvelope;
    if (first.payload.keyset_revision !== 1 || first.payload.parent_keyset_revision !== null) {
      throw policyVerificationError("policy_keyset_revision_invalid");
    }
    if (input.trustedWorkspaceId !== null && first.payload.workspace_id !== input.trustedWorkspaceId) {
      throw policyVerificationError("policy_workspace_mismatch");
    }
    const verified = await verifyKeysetEnvelope(first);
    const currentKey = first.payload.keys.find((key) => key.state === "current");
    if (currentKey === void 0) {
      throw policyVerificationError("policy_keyset_current_invalid");
    }
    const selfSigned = first.signatures.find((signature) => signature.key_id === currentKey.key_id);
    if (selfSigned === void 0 || !await verifyAnySignature(verified, selfSigned.key_id, selfSigned.value)) {
      throw policyVerificationError("policy_signature_invalid");
    }
    for (const signature of first.signatures) {
      const known = keyStateOf(first, signature.key_id) !== null;
      if (!known) {
        throw policyVerificationError("policy_signature_untrusted_key");
      }
    }
    let accepted2 = verified;
    for (const envelope of input.envelopes.slice(1)) {
      accepted2 = await acceptRotation(accepted2, envelope, input.trustedWorkspaceId);
    }
    return {
      acceptedKeyset: accepted2.envelope,
      workspaceId: accepted2.envelope.payload.workspace_id,
      payloadSha256: accepted2.payloadSha256
    };
  }
  let accepted = await verifyKeysetEnvelope(input.trustedKeyset);
  for (const envelope of input.envelopes) {
    accepted = await acceptRotation(accepted, envelope, input.trustedWorkspaceId);
  }
  return {
    acceptedKeyset: accepted.envelope,
    workspaceId: accepted.envelope.payload.workspace_id,
    payloadSha256: accepted.payloadSha256
  };
}
async function acceptRotation(trusted, candidate, trustedWorkspaceId) {
  const validatedCandidate = validateKeysetEnvelope(candidate);
  const candidateRevision = validatedCandidate.payload.keyset_revision;
  const trustedRevision = trusted.envelope.payload.keyset_revision;
  if (validatedCandidate.payload.workspace_id !== trusted.envelope.payload.workspace_id) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  if (trustedWorkspaceId !== null && validatedCandidate.payload.workspace_id !== trustedWorkspaceId) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  if (candidateRevision < trustedRevision) {
    throw policyVerificationError("policy_keyset_downgrade");
  }
  if (candidateRevision === trustedRevision) {
    if (validatedCandidate.payload_sha256 === trusted.envelope.payload_sha256) {
      return trusted;
    }
    throw policyVerificationError("policy_keyset_conflict");
  }
  if (validatedCandidate.payload.parent_keyset_revision !== trustedRevision) {
    throw policyVerificationError("policy_keyset_chain_gap");
  }
  const verified = await verifyKeysetEnvelope(validatedCandidate);
  let chainSignature = false;
  const currentKey = verified.envelope.payload.keys.find((key) => key.state === "current");
  let activationSignature = currentKey === void 0;
  for (const signature of verified.envelope.signatures) {
    const previousState = keyStateOf(trusted.envelope, signature.key_id);
    const nextState = keyStateOf(verified.envelope, signature.key_id);
    const valid = await verifyAnySignature(verified, signature.key_id, signature.value);
    if (!valid) {
      continue;
    }
    if (previousState !== null && previousState !== "retired" && nextState !== null && nextState !== "retired") {
      chainSignature = true;
    }
    if (currentKey !== void 0 && signature.key_id === currentKey.key_id) {
      activationSignature = true;
    }
  }
  if (!chainSignature) {
    throw policyVerificationError("policy_signature_untrusted_key");
  }
  if (!activationSignature) {
    throw policyVerificationError("policy_signature_invalid");
  }
  return verified;
}
function resolveTrustedKey(trustedKeyset, keyId) {
  const keyEntry = trustedKeyset.payload.keys.find((key) => key.key_id === keyId);
  if (keyEntry === void 0) {
    return null;
  }
  return decodeBase64UrlWithoutPadding(keyEntry.public_key);
}

// src/exclusion-policy/snapshot.ts
var UUID_PATTERN12 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var TIMESTAMP_PATTERN2 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
var HEX_SHA256_PATTERN2 = /^[0-9a-f]{64}$/;
var OPERAND_BY_KIND = {
  exact_source_id: "source_id",
  folder_prefix: "folder_prefix",
  path_glob: "path_glob",
  extension: "extension",
  media_type: "media_type",
  maximum_size: "maximum_size_bytes",
  source_type: "source_type"
};
function asObject2(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function requireExactMembers2(value, expected) {
  for (const name of Object.keys(value)) {
    if (!expected.includes(name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  for (const name of expected) {
    if (!Object.prototype.hasOwnProperty.call(value, name)) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
}
function requireString2(value) {
  if (typeof value !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function validateRule(value) {
  const rule = asObject2(value);
  if (Object.keys(rule).length !== 3) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!("rule_id" in rule) || !("rule_kind" in rule)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!UUID_PATTERN12.test(requireString2(rule["rule_id"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const ruleKind = requireString2(rule["rule_kind"]);
  const operandName = OPERAND_BY_KIND[ruleKind];
  if (operandName === void 0) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const expected = ["rule_id", "rule_kind", operandName];
  requireExactMembers2(rule, expected);
  if (operandName === "maximum_size_bytes") {
    const sizeBytes = rule[operandName];
    if (typeof sizeBytes !== "number" || !Number.isInteger(sizeBytes) || sizeBytes < 0) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  } else if (typeof rule[operandName] !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return rule;
}
function validateSnapshotPayload(value) {
  const payload = asObject2(value);
  requireExactMembers2(payload, [
    "contract",
    "workspace_id",
    "policy_revision_id",
    "revision_number",
    "parent_policy_revision_id",
    "published_at",
    "default_decision",
    "evaluator_contract_sha256",
    "rules"
  ]);
  if (payload["contract"] !== SNAPSHOT_PAYLOAD_CONTRACT) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (payload["default_decision"] !== "allowed") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  for (const uuidMember of ["workspace_id", "policy_revision_id"]) {
    if (!UUID_PATTERN12.test(requireString2(payload[uuidMember]))) {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  const parent = payload["parent_policy_revision_id"];
  if (parent !== null && (typeof parent !== "string" || !UUID_PATTERN12.test(parent))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const revisionNumber = payload["revision_number"];
  if (typeof revisionNumber !== "number" || !Number.isInteger(revisionNumber) || revisionNumber < 1) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!TIMESTAMP_PATTERN2.test(requireString2(payload["published_at"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!HEX_SHA256_PATTERN2.test(requireString2(payload["evaluator_contract_sha256"]))) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (!Array.isArray(payload["rules"]) || payload["rules"].length > MAXIMUM_RULES_PER_REVISION) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const rules = payload["rules"].map((rule) => validateRule(rule));
  return {
    contract: SNAPSHOT_PAYLOAD_CONTRACT,
    workspace_id: requireString2(payload["workspace_id"]),
    policy_revision_id: requireString2(payload["policy_revision_id"]),
    revision_number: revisionNumber,
    parent_policy_revision_id: parent,
    published_at: requireString2(payload["published_at"]),
    default_decision: "allowed",
    evaluator_contract_sha256: requireString2(payload["evaluator_contract_sha256"]),
    rules
  };
}
function validateSnapshotEnvelope(value) {
  const envelope = asObject2(value);
  requireExactMembers2(envelope, ["payload", "payload_sha256", "signature"]);
  const payload = validateSnapshotPayload(envelope["payload"]);
  const payloadSha256 = requireString2(envelope["payload_sha256"]);
  if (!HEX_SHA256_PATTERN2.test(payloadSha256)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const signature = asObject2(envelope["signature"]);
  requireExactMembers2(signature, ["algorithm", "key_id", "value"]);
  if (signature["algorithm"] !== SIGNATURE_ALGORITHM) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const keyId = requireString2(signature["key_id"]);
  const signatureValue = requireString2(signature["value"]);
  if (!isWellFormedEd25519KeyId(keyId)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  if (signatureValue.length !== 86 || !/^[A-Za-z0-9_-]+$/.test(signatureValue)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    payload,
    payload_sha256: payloadSha256,
    signature: { algorithm: SIGNATURE_ALGORITHM, key_id: keyId, value: signatureValue }
  };
}
async function verifyPolicySnapshot(input) {
  const envelope = validateSnapshotEnvelope(input.envelope);
  if (envelope.payload.workspace_id !== input.expectedWorkspaceId) {
    throw policyVerificationError("policy_workspace_mismatch");
  }
  const payloadBytes = canonicalJsonBytes(envelope.payload);
  const payloadSha256 = await sha256Hex(payloadBytes);
  if (payloadSha256 !== envelope.payload_sha256) {
    throw policyVerificationError("policy_payload_hash_mismatch");
  }
  const evaluatorContractHash = await sha256Hex(new TextEncoder().encode(EVALUATOR_CONTRACT));
  if (evaluatorContractHash !== envelope.payload.evaluator_contract_sha256) {
    throw policyVerificationError("policy_evaluator_contract_mismatch");
  }
  const publicKey = resolveTrustedKey(input.trustedKeyset, envelope.signature.key_id);
  if (publicKey === null) {
    throw policyVerificationError("policy_signature_untrusted_key");
  }
  const signature = decodeBase64UrlWithoutPadding(envelope.signature.value);
  if (signature === null) {
    throw policyVerificationError("policy_signature_malformed");
  }
  const message = buildSignedPolicyMessage(SNAPSHOT_SIGNING_DOMAIN, payloadBytes);
  const isValid = await verifyDetachedEd25519(message, signature, publicKey);
  if (!isValid) {
    throw policyVerificationError("policy_signature_invalid");
  }
  return { envelope, payloadBytes, payloadSha256 };
}
function resolveSnapshotMonotonicity(candidate, accepted) {
  if (accepted === null) {
    return "accept";
  }
  if (candidate.payload.revision_number > accepted.payload.revision_number) {
    return "accept";
  }
  if (candidate.payload.revision_number < accepted.payload.revision_number) {
    return "downgrade";
  }
  const identical = candidate.payload.policy_revision_id === accepted.payload.policy_revision_id && candidate.payload_sha256 === accepted.payload_sha256;
  return identical ? "identical" : "conflict";
}

// src/exclusion-policy/policy-cache.ts
var POLICY_CACHE_RECORD_CONTRACT = "obsidian_exclusion_policy_cache/v1";
function buildPolicyCacheRecord(state) {
  return {
    contract: POLICY_CACHE_RECORD_CONTRACT,
    workspace_id: state.workspaceId,
    keyset_sequence: state.keysetSequence,
    revision_number: state.revisionNumber,
    policy_revision_id: state.snapshotEnvelope.payload.policy_revision_id,
    payload_sha256: state.snapshotEnvelope.payload_sha256,
    keyset_envelope: state.keysetEnvelope,
    snapshot_envelope: state.snapshotEnvelope
  };
}
function requireMember(record, name) {
  if (!Object.prototype.hasOwnProperty.call(record, name)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return record[name];
}
function requireStringMember(record, name) {
  const value = requireMember(record, name);
  if (typeof value !== "string") {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
function requireIntegerMember(record, name) {
  const value = requireMember(record, name);
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return value;
}
async function readAcceptedPolicyStateFromRecord(record) {
  if (typeof record !== "object" || record === null || Array.isArray(record)) {
    return null;
  }
  const candidate = record;
  try {
    if (requireStringMember(candidate, "contract") !== POLICY_CACHE_RECORD_CONTRACT) {
      return null;
    }
    const workspaceId = requireStringMember(candidate, "workspace_id");
    const keysetSequence = requireIntegerMember(candidate, "keyset_sequence");
    const revisionNumber = requireIntegerMember(candidate, "revision_number");
    const keysetEnvelope = validateKeysetEnvelope(requireMember(candidate, "keyset_envelope"));
    const snapshotEnvelope = validateSnapshotEnvelope(requireMember(candidate, "snapshot_envelope"));
    await verifyKeysetEnvelopeIntegrity(keysetEnvelope);
    if (snapshotEnvelope.payload.workspace_id !== workspaceId) {
      return null;
    }
    if (keysetEnvelope.payload.workspace_id !== workspaceId) {
      return null;
    }
    await verifyPolicySnapshot({
      envelope: snapshotEnvelope,
      trustedKeyset: keysetEnvelope,
      expectedWorkspaceId: workspaceId
    });
    return {
      workspaceId,
      revisionNumber,
      keysetSequence,
      keysetEnvelope,
      snapshotEnvelope
    };
  } catch {
    return null;
  }
}
async function persistAcceptedPolicyState(state, adapter) {
  const record = buildPolicyCacheRecord(state);
  let canonicalForm;
  try {
    canonicalForm = canonicalizeClosedJson(record);
  } catch {
    throw policyVerificationError("policy_cache_write_failed");
  }
  try {
    await adapter.writePolicyCacheRecord(record);
  } catch {
    throw policyVerificationError("policy_cache_write_failed");
  }
  let readBack;
  try {
    readBack = await adapter.readPolicyCacheRecord();
  } catch {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
  let readBackForm;
  try {
    readBackForm = canonicalizeClosedJson(readBack);
  } catch {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
  if (readBackForm !== canonicalForm) {
    throw policyVerificationError("policy_cache_readback_mismatch");
  }
}

// src/exclusion-policy/policy-session.ts
var KEYSET_PAGE_MAXIMUM_BYTES = 1024 * 1024;
var SNAPSHOT_RESPONSE_MAXIMUM_BYTES = SIGNED_SNAPSHOT_MAXIMUM_BYTES + 1024;
function isTransientStatus(status) {
  return status === 429 || status >= 500;
}
function parseWireEnvelope(response, maximumBytes) {
  let parsed;
  try {
    parsed = parseClosedJson(response.bodyText, { maximumBytes });
  } catch (error2) {
    if (isTransientStatus(response.status)) {
      throw policyVerificationError("policy_network_unavailable");
    }
    throw error2;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw policyVerificationError("policy_envelope_invalid");
  }
  const envelope = parsed;
  for (const name of Object.keys(envelope)) {
    if (name !== "data" && name !== "error" && name !== "request_id" && name !== "warnings") {
      throw policyVerificationError("policy_envelope_invalid");
    }
  }
  for (const required of ["data", "error", "request_id", "warnings"]) {
    if (!Object.prototype.hasOwnProperty.call(envelope, required)) {
      throw policyVerificationError("policy_envelope_invalid");
    }
  }
  const error = envelope["error"];
  if (error !== null) {
    if (typeof error !== "object" || error === null || Array.isArray(error)) {
      throw policyVerificationError("policy_envelope_invalid");
    }
    const code = error["code"];
    if (code === "exclusion_policy_not_initialized") {
      throw policyVerificationError("policy_not_initialized_on_server");
    }
    if (isTransientStatus(response.status)) {
      throw policyVerificationError("policy_network_unavailable");
    }
    throw policyVerificationError("policy_envelope_invalid");
  }
  if (envelope["data"] === null || envelope["data"] === void 0) {
    throw policyVerificationError("policy_envelope_invalid");
  }
  return envelope["data"];
}
function validateKeysetPage(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  const page = value;
  for (const name of Object.keys(page)) {
    if (name !== "has_more" && name !== "keysets") {
      throw policyVerificationError("policy_payload_schema_invalid");
    }
  }
  if (typeof page["has_more"] !== "boolean" || !Array.isArray(page["keysets"])) {
    throw policyVerificationError("policy_payload_schema_invalid");
  }
  return {
    has_more: page["has_more"],
    keysets: page["keysets"].map((envelope) => validateKeysetEnvelope(envelope))
  };
}
async function normalizeSnapshotRules(snapshot) {
  const rules = [];
  for (const rule of snapshot.payload.rules) {
    rules.push(
      await normalizePolicyRule({
        ruleId: rule.rule_id,
        ruleKind: rule.rule_kind,
        sourceIdOperand: rule.source_id ?? null,
        textOperand: rule.folder_prefix ?? rule.path_glob ?? rule.extension ?? rule.media_type ?? rule.source_type ?? null,
        sizeBytesOperand: rule.maximum_size_bytes ?? null
      })
    );
  }
  return rules;
}
var PolicySession = class {
  #deps;
  #state = "policy_not_initialized";
  #accepted = null;
  #normalizedRules = null;
  constructor(deps) {
    this.#deps = deps;
  }
  get state() {
    return this.#state;
  }
  get acceptedState() {
    return this.#accepted;
  }
  #setState(state) {
    this.#state = state;
    this.#deps.onStateChange?.(state);
  }
  /** Load and re-verify the persisted cache into memory (offline startup). */
  async restoreFromCache() {
    const record = await this.#deps.cache.readPolicyCacheRecord();
    const restored = await readAcceptedPolicyStateFromRecord(record);
    if (restored === null) {
      this.#accepted = null;
      this.#normalizedRules = null;
      this.#setState("policy_not_initialized");
      return;
    }
    try {
      this.#normalizedRules = await normalizeSnapshotRules(restored.snapshotEnvelope);
    } catch {
      this.#accepted = null;
      this.#normalizedRules = null;
      this.#setState("policy_not_initialized");
      return;
    }
    this.#accepted = restored;
    this.#setState("policy_offline_cached");
  }
  async #request(path, headers) {
    const token = this.#deps.getAccessToken();
    if (token === null) {
      throw policyVerificationError("policy_network_unavailable");
    }
    const origin = this.#deps.resolveOrigin();
    if (origin === "") {
      throw policyVerificationError("policy_network_unavailable");
    }
    const request = {
      url: `${origin}${path}`,
      headers: { accept: "application/json", authorization: `Bearer ${token}`, ...headers }
    };
    try {
      return await this.#deps.http(request);
    } catch {
      throw policyVerificationError("policy_network_unavailable");
    }
  }
  async #fetchKeysetEnvelopes(afterRevision) {
    const collected = [];
    let cursor = afterRevision;
    for (let fetchIndex = 0; fetchIndex < KEYSET_PAGE_MAXIMUM_FETCHES; fetchIndex += 1) {
      const response = await this.#request(
        `/api/sync/exclusion-policy/keysets?after_keyset_revision=${cursor}`,
        {}
      );
      const page = validateKeysetPage(
        parseWireEnvelope(response, KEYSET_PAGE_MAXIMUM_BYTES)
      );
      collected.push(...page.keysets);
      if (!page.has_more) {
        return collected;
      }
      const lastRevision = page.keysets[page.keysets.length - 1]?.payload.keyset_revision;
      if (lastRevision === void 0 || lastRevision <= cursor) {
        throw policyVerificationError("policy_keyset_page_overflow");
      }
      cursor = lastRevision;
    }
    throw policyVerificationError("policy_keyset_page_overflow");
  }
  async #fetchSnapshot(etag) {
    const headers = {};
    if (etag !== null) {
      headers["if-none-match"] = etag;
    }
    const response = await this.#request("/api/sync/exclusion-policy/snapshot", headers);
    if (response.status === 304) {
      return { unchanged: true };
    }
    const envelope = validateSnapshotEnvelope(
      parseWireEnvelope(response, SNAPSHOT_RESPONSE_MAXIMUM_BYTES)
    );
    return { unchanged: false, envelope };
  }
  /**
   * Initial trust: accept the self-signed keyset revision 1 and the active
   * snapshot ONLY immediately after authenticated device onboarding. A
   * completed re-onboarding is the one boundary that may REPLACE an existing
   * anchor (e.g. after disconnecting and pointing at another workspace); the
   * replacement is fully verified before the single record is rewritten, and
   * any failure of the new candidate preserves the prior anchor and cache.
   */
  async adoptOnboardingTrust() {
    try {
      const envelopes = await this.#fetchKeysetEnvelopes(0);
      const chain = await verifyKeysetChain({
        envelopes,
        trustedKeyset: null,
        trustedWorkspaceId: null,
        allowInitialTrust: true
      });
      const snapshotResponse = await this.#fetchSnapshot(null);
      if (snapshotResponse.unchanged) {
        throw policyVerificationError("policy_envelope_invalid");
      }
      const verifiedSnapshot = await verifyPolicySnapshot({
        envelope: snapshotResponse.envelope,
        trustedKeyset: chain.acceptedKeyset,
        expectedWorkspaceId: chain.workspaceId
      });
      const candidate = {
        workspaceId: chain.workspaceId,
        revisionNumber: verifiedSnapshot.envelope.payload.revision_number,
        keysetSequence: chain.acceptedKeyset.payload.keyset_revision,
        keysetEnvelope: chain.acceptedKeyset,
        snapshotEnvelope: verifiedSnapshot.envelope
      };
      const normalizedRules = await normalizeSnapshotRules(verifiedSnapshot.envelope);
      await persistAcceptedPolicyState(candidate, this.#deps.cache);
      this.#accepted = candidate;
      this.#normalizedRules = normalizedRules;
      this.#setState("policy_ready");
    } catch (error) {
      this.#handleAcquisitionFailure(error);
      throw error;
    }
  }
  /**
   * Session refresh: check the server snapshot (conditional GET), fetch and
   * verify the keyset chain first when the snapshot key is unknown, verify
   * into temporary memory and atomically replace the cache only after every
   * check passes. Never clears a good cache on failure.
   */
  async refresh() {
    if (this.#state === "policy_integrity_failed") {
      return;
    }
    const etag = this.#accepted === null ? null : `"${this.#accepted.snapshotEnvelope.payload_sha256}"`;
    try {
      const snapshotResponse = await this.#fetchSnapshot(etag);
      if (snapshotResponse.unchanged) {
        if (this.#accepted !== null) {
          this.#setState("policy_ready");
          return;
        }
        throw policyVerificationError("policy_envelope_invalid");
      }
      const snapshotEnvelope = validateSnapshotEnvelope(snapshotResponse.envelope);
      let trustedKeyset = this.#accepted?.keysetEnvelope ?? null;
      let trustedWorkspaceId = this.#accepted?.workspaceId ?? null;
      if (trustedKeyset === null) {
        const envelopes = await this.#fetchKeysetEnvelopes(0);
        const chain = await verifyKeysetChain({
          envelopes,
          trustedKeyset: null,
          trustedWorkspaceId: null,
          allowInitialTrust: false
        });
        trustedKeyset = chain.acceptedKeyset;
        trustedWorkspaceId = chain.workspaceId;
      } else {
        const knownKey = trustedKeyset.payload.keys.some(
          (key) => key.key_id === snapshotEnvelope.signature.key_id
        );
        if (!knownKey) {
          const envelopes = await this.#fetchKeysetEnvelopes(
            trustedKeyset.payload.keyset_revision
          );
          const chain = await verifyKeysetChain({
            envelopes,
            trustedKeyset,
            trustedWorkspaceId,
            allowInitialTrust: false
          });
          trustedKeyset = chain.acceptedKeyset;
        }
      }
      const verifiedSnapshot = await verifyPolicySnapshot({
        envelope: snapshotEnvelope,
        trustedKeyset,
        expectedWorkspaceId: trustedWorkspaceId ?? snapshotEnvelope.payload.workspace_id
      });
      const monotonicity = resolveSnapshotMonotonicity(
        verifiedSnapshot.envelope,
        this.#accepted?.snapshotEnvelope ?? null
      );
      if (monotonicity === "downgrade") {
        throw policyVerificationError("policy_snapshot_downgrade");
      }
      if (monotonicity === "conflict") {
        throw policyVerificationError("policy_snapshot_conflict");
      }
      if (monotonicity === "identical") {
        if (this.#accepted !== null) {
          this.#setState("policy_ready");
          return;
        }
      }
      const candidate = {
        workspaceId: verifiedSnapshot.envelope.payload.workspace_id,
        revisionNumber: verifiedSnapshot.envelope.payload.revision_number,
        keysetSequence: trustedKeyset.payload.keyset_revision,
        keysetEnvelope: trustedKeyset,
        snapshotEnvelope: verifiedSnapshot.envelope
      };
      const normalizedRules = await normalizeSnapshotRules(verifiedSnapshot.envelope);
      await persistAcceptedPolicyState(candidate, this.#deps.cache);
      this.#accepted = candidate;
      this.#normalizedRules = normalizedRules;
      this.#setState("policy_ready");
    } catch (error) {
      this.#handleAcquisitionFailure(error);
      throw error;
    }
  }
  #handleAcquisitionFailure(error) {
    if (!(error instanceof PolicyVerificationError)) {
      this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_offline_cached");
      return;
    }
    switch (error.reason) {
      case "policy_network_unavailable":
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_offline_cached");
        return;
      case "policy_not_initialized_on_server":
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_refresh_required");
        return;
      case "policy_cache_write_failed":
      case "policy_cache_readback_mismatch":
        this.#setState(this.#accepted === null ? "policy_not_initialized" : "policy_refresh_required");
        return;
      default:
        this.#setState("policy_integrity_failed");
    }
  }
  /**
   * Local deny-only evaluation against the last accepted snapshot. Any absent,
   * untrusted or unnormalizable policy, and any invalid subject evidence,
   * fails closed to the enforced excluded decision.
   */
  evaluate(subject) {
    if (this.#accepted === null || this.#normalizedRules === null) {
      return { raw: "indeterminate", enforced: "excluded" };
    }
    try {
      const outcome = evaluatePolicy(this.#normalizedRules, {
        ...subject,
        workspaceId: this.#accepted.workspaceId
      }, { workspaceId: this.#accepted.workspaceId });
      if (outcome.raw === "allowed") {
        return { raw: "allowed", enforced: "allowed" };
      }
      return { raw: outcome.raw, enforced: "excluded" };
    } catch {
      return { raw: "indeterminate", enforced: "excluded" };
    }
  }
  /**
   * The one narrow capture seam (journal design 7.1, 9): the fail-closed
   * local decision for an observed file together with the accepted policy
   * revision the decision was taken under. The plugin journal persists that
   * revision on its capture rows; the server still re-evaluates policy
   * itself and never trusts this value.
   */
  evaluateForCapture(subject) {
    return {
      decision: this.evaluate(subject),
      revisionNumber: this.#accepted?.revisionNumber ?? 0
    };
  }
};

// src/conflicts/ConflictInboxModal.ts
var import_obsidian4 = require("obsidian");
function conflictKindLabel(kind) {
  switch (kind) {
    case "stale_content":
      return "Content conflict";
    case "edit_remote_delete":
      return "Edit vs remote delete";
    case "delete_remote_edit":
      return "Delete vs remote edit";
    case "locator_collision":
      return "Path collision";
  }
}
function outcomeText(result) {
  switch (result.kind) {
    case "resolved_and_applied":
      return "Resolved \u2014 canonical outcome applied.";
    case "local_apply_pending":
      return "Canonical outcome pending local apply \u2014 safe retry scheduled.";
    case "stale_successor":
      return "Remote changed \u2014 a successor conflict requires review.";
  }
}
function failureText(error, subject) {
  if (error instanceof ConflictApiError) {
    return `${subject} failed: ${error.kind}`;
  }
  if (error instanceof ConflictControllerError) {
    return `${subject} failed: ${error.reason}`;
  }
  return `${subject} failed: reason_unavailable`;
}
var ConflictInboxModal = class extends import_obsidian4.Modal {
  #controller;
  /**
   * The live merge editor element — the modal's ONLY draft storage. The
   * draft lives in this bounded ephemeral memory while the editor is open
   * and is cleared on discard or close; nothing is persisted anywhere.
   */
  #mergeEditor = null;
  #renderTask = Promise.resolve();
  constructor(app, controller) {
    super(app);
    this.#controller = controller;
  }
  /** Await the current render/command task (the composition and test seam). */
  awaitRendered() {
    return this.#renderTask;
  }
  onOpen() {
    this.#run(() => this.#renderList());
  }
  onClose() {
    this.#discardDraft();
  }
  /** Run one async UI task under the render tracking; failures render the closed sentence. */
  #run(task) {
    this.#renderTask = task().catch((error) => {
      this.#renderOutcome(failureText(error, "Inbox"));
    });
  }
  #resetContent(title) {
    this.#discardDraft();
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(title);
  }
  #discardDraft() {
    this.#mergeEditor = null;
  }
  async #renderList() {
    this.#discardDraft();
    const page = await this.#controller.listOpenConflicts();
    this.#resetContent("Conflict Inbox");
    if (page.conflicts.length === 0) {
      this.contentEl.createEl("p", { text: "No open conflicts." });
      return;
    }
    for (const conflict of page.conflicts) {
      const label = `Open: ${conflictKindLabel(conflict.conflictKind)}`;
      new import_obsidian4.Setting(this.contentEl).addButton(
        (button) => button.setButtonText(label).setCta().onClick(() => {
          this.#run(() => this.#renderDetail(conflict.conflictId));
        })
      );
    }
  }
  async #renderDetail(conflictId) {
    this.#discardDraft();
    const detail = await this.#controller.getConflictDetail(conflictId);
    this.#resetContent(`Conflict: ${conflictKindLabel(detail.conflictKind)}`);
    if (detail.choices.includes("keep_remote")) {
      new import_obsidian4.Setting(this.contentEl).addButton(
        (button) => button.setButtonText("Keep remote").setCta().onClick(() => {
          this.#run(async () => {
            await this.#executeCommand(() => this.#controller.resolveKeepRemote(conflictId));
          });
        })
      );
    }
    if (detail.choices.includes("keep_local")) {
      new import_obsidian4.Setting(this.contentEl).addButton(
        (button) => button.setButtonText("Keep local").setCta().onClick(() => {
          this.#run(async () => {
            await this.#executeCommand(() => this.#controller.resolveKeepLocal(conflictId));
          });
        })
      );
    }
    if (detail.choices.includes("save_merged")) {
      new import_obsidian4.Setting(this.contentEl).addButton(
        (button) => button.setButtonText("Edit merged result\u2026").setCta().onClick(() => {
          this.#run(() => this.#renderMergeEditor(conflictId));
        })
      );
    }
    new import_obsidian4.Setting(this.contentEl).addButton(
      (button) => button.setButtonText("Back").onClick(() => {
        this.#run(() => this.#renderList());
      })
    );
  }
  async #renderMergeEditor(conflictId) {
    const proposal = await this.#controller.buildMergeProposal(conflictId);
    if (proposal.kind !== "editable_merge") {
      this.#resetContent(`Conflict: manual choice required`);
      this.contentEl.createEl("p", {
        text: `Manual choice required \u2014 keep remote or keep local (${proposal.reason}).`
      });
      new import_obsidian4.Setting(this.contentEl).addButton(
        (button) => button.setButtonText("Back").onClick(() => {
          this.#run(() => this.#renderDetail(conflictId));
        })
      );
      return;
    }
    this.#resetContent("Resolve by merged result");
    if (proposal.requiresUserReview) {
      this.contentEl.createEl("p", {
        text: "Conflicting regions need your review \u2014 edit the marked hunks before saving."
      });
    }
    const editor = this.contentEl.createEl("textarea");
    editor.value = proposal.mergedText;
    editor.style.width = "100%";
    editor.style.minHeight = "16rem";
    this.#mergeEditor = editor;
    new import_obsidian4.Setting(this.contentEl).addButton(
      (button) => button.setButtonText("Save merged").setCta().onClick(() => {
        const editedText = editor.value;
        this.#run(async () => {
          await this.#executeCommand(
            () => this.#controller.resolveSaveMerged(conflictId, editedText)
          );
        });
      })
    );
    new import_obsidian4.Setting(this.contentEl).addButton(
      (button) => button.setButtonText("Discard draft").onClick(() => {
        this.#discardDraft();
        this.#run(() => this.#renderDetail(conflictId));
      })
    );
  }
  /** Execute one resolution command and render its closed outcome sentence. */
  async #executeCommand(command) {
    try {
      const result = await command();
      this.#renderOutcome(outcomeText(result));
    } catch (error) {
      this.#renderOutcome(failureText(error, "Resolution"));
    }
  }
  #renderOutcome(text) {
    this.#discardDraft();
    this.#resetContent("Conflict Inbox");
    this.contentEl.createEl("p", { text });
    new import_obsidian4.Setting(this.contentEl).addButton(
      (button) => button.setButtonText("Back to inbox").onClick(() => {
        this.#run(() => this.#renderList());
      })
    );
  }
};

// src/conflicts/repository.ts
var UUID_PATTERN13 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function isUuid5(value) {
  return typeof value === "string" && UUID_PATTERN13.test(value);
}
function isNonNegativeInteger5(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function isClosedToken3(value, closedSet) {
  return typeof value === "string" && closedSet.includes(value);
}
function sqlText6(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
function firstRow5(result) {
  return result[0]?.values[0] ?? null;
}
var CONFLICT_LOCAL_REPAIR_COLUMNS = [
  "conflict_id",
  "resolution_event_id",
  "target_action",
  "safe_reason",
  "attempt_count",
  "next_eligible_retry_epoch_ms",
  "created_at_epoch_ms",
  "updated_at_epoch_ms"
];
function parsePendingLocalApplyRow(row) {
  const [
    conflictId,
    resolutionEventId,
    targetAction,
    safeReason,
    attemptCount,
    nextEligibleRetryEpochMs,
    createdAtEpochMs,
    updatedAtEpochMs
  ] = row;
  if (!isUuid5(conflictId) || !isUuid5(resolutionEventId) || !isClosedToken3(targetAction, CONFLICT_LOCAL_REPAIR_ACTIONS) || !isClosedToken3(safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) || !isNonNegativeInteger5(attemptCount) || !(nextEligibleRetryEpochMs === null || isNonNegativeInteger5(nextEligibleRetryEpochMs)) || !isNonNegativeInteger5(createdAtEpochMs) || !isNonNegativeInteger5(updatedAtEpochMs)) {
    throw journalStoreError("journal_query_failed");
  }
  return {
    conflictId,
    resolutionEventId,
    targetAction,
    safeReason,
    attemptCount,
    nextEligibleRetryEpochMs,
    createdAtEpochMs,
    updatedAtEpochMs
  };
}
var ConflictRepository = class {
  #database;
  constructor(options) {
    this.#database = options.database;
  }
  /** One pending local apply by its conflict identity, or null (read-only). */
  readPendingLocalApply(conflictId) {
    if (!isUuid5(conflictId)) {
      throw journalStoreError("journal_query_failed");
    }
    const row = firstRow5(
      this.#database.readAll(
        [
          `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
          `where conflict_id = ${sqlText6(conflictId)};`
        ].join(" ")
      )
    );
    return row === null ? null : parsePendingLocalApplyRow(row);
  }
  /** Every pending local apply, oldest first (read-only). */
  readPendingLocalApplies() {
    const rows = this.#database.readAll(
      [
        `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
        "order by created_at_epoch_ms asc, conflict_id asc;"
      ].join(" ")
    );
    return (rows[0]?.values ?? []).map((row) => parsePendingLocalApplyRow(row));
  }
  /**
   * Park one pending local apply fact (spec 5.2.6). An exact re-park
   * under the SAME resolution event identity refreshes the safe reason
   * and bookkeeping idempotently (the crash/retry replay); a re-park
   * under a FOREIGN resolution event contradicts the durable canonical
   * outcome and refuses — the resolution identity never forks locally.
   */
  async parkPendingLocalApply(input) {
    validateParkInput(input);
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null) {
        session.exec(
          [
            "insert into conflict_local_repairs (conflict_id, resolution_event_id,",
            "target_action, safe_reason, attempt_count, next_eligible_retry_epoch_ms,",
            "created_at_epoch_ms, updated_at_epoch_ms) values (",
            `${sqlText6(input.conflictId)}, ${sqlText6(input.resolutionEventId)},`,
            `${sqlText6(input.targetAction)}, ${sqlText6(input.safeReason)},`,
            "0, null,",
            `${input.nowEpochMs}, ${input.nowEpochMs});`
          ].join(" ")
        );
        return;
      }
      if (existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update conflict_local_repairs set",
          `target_action = ${sqlText6(input.targetAction)},`,
          `safe_reason = ${sqlText6(input.safeReason)},`,
          `updated_at_epoch_ms = ${input.nowEpochMs}`,
          `where conflict_id = ${sqlText6(input.conflictId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Record one failed local-apply attempt: the attempt count grows, the
   * closed safe reason updates and the next eligible retry moment parks
   * the row. A missing row or a foreign resolution event refuses.
   */
  async recordLocalApplyFailure(input) {
    if (!isUuid5(input.conflictId) || !isUuid5(input.resolutionEventId) || !isClosedToken3(input.safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) || !isNonNegativeInteger5(input.nowEpochMs) || !isNonNegativeInteger5(input.nextEligibleRetryEpochMs)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null || existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(
        [
          "update conflict_local_repairs set",
          `safe_reason = ${sqlText6(input.safeReason)},`,
          `attempt_count = ${existing.attemptCount + 1},`,
          `next_eligible_retry_epoch_ms = ${input.nextEligibleRetryEpochMs},`,
          `updated_at_epoch_ms = ${input.nowEpochMs}`,
          `where conflict_id = ${sqlText6(input.conflictId)};`
        ].join(" ")
      );
    });
  }
  /**
   * Complete one pending local apply by deleting its row. Only the
   * matching resolution event may complete the parked fact: a foreign
   * identity refuses and keeps the owed work visible.
   */
  async completeLocalApply(input) {
    if (!isUuid5(input.conflictId) || !isUuid5(input.resolutionEventId)) {
      throw journalStoreError("journal_mutation_failed");
    }
    await this.#database.runSerializedMutation((session) => {
      const existing = this.#readRepairRow(session, input.conflictId);
      if (existing === null || existing.resolutionEventId !== input.resolutionEventId) {
        throw journalStoreError("journal_mutation_failed");
      }
      session.exec(`delete from conflict_local_repairs where conflict_id = ${sqlText6(input.conflictId)};`);
    });
  }
  // --- internals --------------------------------------------------------------------------------------
  #readRepairRow(session, conflictId) {
    const row = firstRow5(
      session.readRows(
        [
          `select ${CONFLICT_LOCAL_REPAIR_COLUMNS.join(", ")} from conflict_local_repairs`,
          `where conflict_id = ${sqlText6(conflictId)};`
        ].join(" ")
      )
    );
    return row === null ? null : parsePendingLocalApplyRow(row);
  }
};
function validateParkInput(input) {
  if (!isUuid5(input.conflictId) || !isUuid5(input.resolutionEventId) || !isClosedToken3(input.targetAction, CONFLICT_LOCAL_REPAIR_ACTIONS) || !isClosedToken3(input.safeReason, CONFLICT_LOCAL_REPAIR_SAFE_REASONS) || !isNonNegativeInteger5(input.nowEpochMs)) {
    throw journalStoreError("journal_mutation_failed");
  }
}

// src/device-sync/atomic-vault-writer.ts
var AtomicVaultWriterError = class extends Error {
  stage;
  reason;
  retryable;
  restoredToBase;
  constructor(stage, reason, retryable, restoredToBase) {
    super(`atomic vault writer failed: ${reason}`);
    this.name = "AtomicVaultWriterError";
    this.stage = stage;
    this.reason = reason;
    this.retryable = retryable;
    this.restoredToBase = restoredToBase;
  }
};
function writerError(stage, reason, retryable, restoredToBase = false) {
  return new AtomicVaultWriterError(stage, reason, retryable, restoredToBase);
}
function storeReasonOf2(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string") {
      return reason;
    }
  }
  return null;
}
async function hashesTo2(bytes, fingerprint) {
  if (bytes === null || fingerprint === null) {
    return false;
  }
  return bytes.byteLength === fingerprint.sizeBytes && await sha256Hex(bytes) === fingerprint.sha256;
}
function toArrayBuffer3(bytes) {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
function isHiddenSiblingLocator(locator) {
  const lastSlash = locator.lastIndexOf("/");
  const baseName = lastSlash === -1 ? locator : locator.slice(lastSlash + 1);
  return baseName.startsWith(".");
}
function createStructuralVaultMutationSeam(vault, adapter) {
  const adapterOf = () => {
    if (adapter === void 0) {
      throw writerError("vault_mutation", "device_apply_vault_failed", true);
    }
    return adapter;
  };
  return {
    async locatorExists(locator) {
      if (isHiddenSiblingLocator(locator)) {
        return await adapterOf().exists(locator);
      }
      if (vault.getAbstractFileByPath(locator) !== null) {
        return true;
      }
      return adapter === void 0 ? false : await adapter.exists(locator);
    },
    async createFile(locator, bytes) {
      if (isHiddenSiblingLocator(locator)) {
        await adapterOf().writeBinary(locator, toArrayBuffer3(bytes));
        return;
      }
      await vault.createBinary(locator, toArrayBuffer3(bytes));
    },
    async readBytes(locator) {
      if (isHiddenSiblingLocator(locator)) {
        if (!await adapterOf().exists(locator)) {
          return null;
        }
        return new Uint8Array(await adapterOf().readBinary(locator));
      }
      if (vault.getAbstractFileByPath(locator) !== null) {
        return new Uint8Array(await vault.readBinary(locator));
      }
      if (adapter === void 0) {
        return null;
      }
      if (!await adapter.exists(locator)) {
        return null;
      }
      return new Uint8Array(await adapter.readBinary(locator));
    },
    async renameLocator(fromLocator, toLocator) {
      if (isHiddenSiblingLocator(fromLocator) || isHiddenSiblingLocator(toLocator)) {
        await adapterOf().rename(fromLocator, toLocator);
        return;
      }
      const file = vault.getAbstractFileByPath(fromLocator);
      if (file === null) {
        throw writerError("vault_mutation", "device_apply_vault_failed", true);
      }
      await vault.rename(file, toLocator);
    },
    async trashLocator(locator) {
      if (isHiddenSiblingLocator(locator)) {
        await adapterOf().remove(locator);
        return;
      }
      const file = vault.getAbstractFileByPath(locator);
      if (file === null) {
        throw writerError("trash", "device_apply_trash_failed", true);
      }
      await vault.trash(file, false);
    }
  };
}
function contentTargetOf(operation) {
  if (operation.operation === "updated") {
    return operation.priorLocator;
  }
  if (operation.operation === "created" || operation.operation === "restored") {
    return operation.targetLocator;
  }
  return null;
}
var AtomicVaultWriterImpl = class {
  #repository;
  #seam;
  constructor(options) {
    this.#repository = options.repository;
    this.#seam = options.seam;
  }
  async stageAndReplace(input) {
    const target = input.targetLocator;
    const isUpdate = input.operation === "updated";
    if (isUpdate) {
      if (!await this.#seam.locatorExists(target)) {
        throw writerError("vault_mutation", "device_manifest_local_diverged", false);
      }
    } else if (await this.#seam.locatorExists(target)) {
      throw writerError("vault_mutation", "device_manifest_target_occupied", false);
    }
    let durableProofFailure = null;
    let hasDurableProof = input.operation === "restored";
    const seam = this.#seam;
    const sequencedSeam = {
      locatorExists: (locator) => seam.locatorExists(locator),
      createFile: (locator, bytes) => seam.createFile(locator, bytes),
      readBytes: async (locator) => {
        const bytes = await seam.readBytes(locator);
        if (bytes === null && !hasDurableProof && isUpdate && locator === target) {
          throw writerError("vault_mutation", "device_manifest_local_diverged", false);
        }
        return bytes;
      },
      trashLocator: (locator) => seam.trashLocator(locator),
      renameLocator: async (fromLocator, toLocator) => {
        if (!hasDurableProof) {
          hasDurableProof = true;
          try {
            await this.#repository.transitionRemoteApply({
              eventSequence: input.eventSequence,
              state: "temp_verified",
              tempToken: input.tempToken
            });
          } catch (error) {
            durableProofFailure = error;
            throw error;
          }
        }
        await seam.renameLocator(fromLocator, toLocator);
      }
    };
    let mutated;
    try {
      mutated = await stageVerifyAndReplaceVaultContent({
        seam: sequencedSeam,
        targetLocator: target,
        tempToken: input.tempToken,
        bytes: input.bytes,
        expectedFinalFingerprint: input.expectedFinalFingerprint,
        expectedBaseFingerprint: isUpdate ? input.baseFingerprint : null
      });
    } catch (error) {
      if (durableProofFailure !== null) {
        const reason = storeReasonOf2(durableProofFailure) ?? "device_apply_vault_failed";
        throw writerError("verify_temp", reason, false);
      }
      throw this.#mapMutationFailure(error);
    }
    return {
      targetLocator: target,
      verifiedFingerprint: input.expectedFinalFingerprint,
      tempToken: input.tempToken,
      rollbackToken: mutated.rollbackLocator !== null ? input.tempToken : null
    };
  }
  async renameOrMove(input) {
    if (await this.#seam.locatorExists(input.targetLocator)) {
      throw writerError("vault_mutation", "device_manifest_target_occupied", false);
    }
    const priorBytes = await this.#readOrNull(input.priorLocator, "vault_mutation");
    if (priorBytes === null || !await hashesTo2(priorBytes, input.expectedFinalFingerprint)) {
      throw writerError("vault_mutation", "device_manifest_local_diverged", false);
    }
    try {
      await this.#seam.renameLocator(input.priorLocator, input.targetLocator);
    } catch (error) {
      throw this.#wrap(error, "vault_mutation", "device_apply_vault_failed", true);
    }
    const finalBytes = await this.#readOrNull(input.targetLocator, "verify_final");
    if (!await hashesTo2(finalBytes, input.expectedFinalFingerprint)) {
      let restoredToBase = false;
      try {
        await this.#seam.renameLocator(input.targetLocator, input.priorLocator);
        restoredToBase = await hashesTo2(
          await this.#seam.readBytes(input.priorLocator),
          input.expectedFinalFingerprint
        );
      } catch {
        restoredToBase = false;
      }
      throw writerError("verify_final", "device_apply_vault_failed", false, restoredToBase);
    }
    return {
      targetLocator: input.targetLocator,
      verifiedFingerprint: input.expectedFinalFingerprint,
      tempToken: null,
      rollbackToken: null
    };
  }
  async trash(input) {
    const priorBytes = await this.#readOrNull(input.priorLocator, "trash");
    if (priorBytes === null) {
      return { targetLocator: null, verifiedFingerprint: null, tempToken: null, rollbackToken: null };
    }
    if (input.baseFingerprint !== null && !await hashesTo2(priorBytes, input.baseFingerprint)) {
      throw writerError("trash", "device_manifest_local_diverged", false);
    }
    try {
      await this.#seam.trashLocator(input.priorLocator);
    } catch (error) {
      throw this.#wrap(error, "trash", "device_apply_trash_failed", true);
    }
    return { targetLocator: null, verifiedFingerprint: null, tempToken: null, rollbackToken: null };
  }
  async recover(operation) {
    switch (operation.state) {
      case "prepared":
        return this.#recoverPrepared(operation);
      case "temp_verified":
        return this.#recoverTempVerified(operation);
      case "vault_mutated":
        return this.#recoverVaultMutated(operation);
      case "locally_applied":
      case "server_acknowledged":
        return {
          kind: "clean",
          eventSequence: operation.eventSequence,
          cleanupFailure: await this.#cleanSiblingsOf(operation)
        };
    }
  }
  // --- internals ---------------------------------------------------------------------------------------
  async #readOrNull(locator, stage) {
    try {
      return await this.#seam.readBytes(locator);
    } catch (error) {
      throw this.#wrap(error, stage, "device_apply_vault_failed", true);
    }
  }
  #wrap(error, stage, reason, retryable) {
    if (error instanceof AtomicVaultWriterError) {
      return error;
    }
    return writerError(stage, reason, retryable);
  }
  /**
   * Map the shared primitive's private typed failure onto the writer's
   * closed stage/reason vocabulary — no new public tokens, the same
   * pairs the inline discipline raised. A prove-base refusal keeps the
   * divergence token (the applier settles it durably as a conflict);
   * the staged-bytes and replace refusals stay retryable vault failures.
   */
  #mapMutationFailure(error) {
    if (error instanceof AtomicVaultMutationFailure) {
      switch (error.stage) {
        case "stage":
        case "verify_staged":
          return writerError("verify_temp", "device_apply_vault_failed", true);
        case "prove_base":
          return writerError("vault_mutation", "device_manifest_local_diverged", false);
        case "replace":
          return writerError("vault_mutation", "device_apply_vault_failed", true);
        case "verify_final":
          return writerError(
            "verify_final",
            "device_apply_vault_failed",
            false,
            error.restoredToBase
          );
      }
    }
    return writerError("vault_mutation", "device_apply_vault_failed", true);
  }
  /** Restore the verified old bytes from the rollback sibling; true only when the base proof passes again. */
  async #restoreRollback(target, rollbackLocator, baseFingerprint) {
    try {
      await this.#seam.trashLocator(target);
      await this.#seam.renameLocator(rollbackLocator, target);
    } catch {
      return false;
    }
    const restoredBytes = await this.#seam.readBytes(target);
    if (baseFingerprint === null) {
      return restoredBytes !== null;
    }
    return hashesTo2(restoredBytes, baseFingerprint);
  }
  /** Best-effort cleanup of the durable siblings one operation names; returns a closed failure reason or null. */
  async #cleanSiblingsOf(operation) {
    const target = contentTargetOf(operation);
    if (target === null || operation.tempToken === null) {
      return null;
    }
    let cleanupFailure = null;
    const tempLocator = buildTempSiblingLocator(target, operation.tempToken);
    const rollbackLocator = buildRollbackSiblingLocator(target, operation.tempToken);
    for (const sibling of [tempLocator, rollbackLocator]) {
      try {
        if (await this.#seam.locatorExists(sibling)) {
          await this.#seam.trashLocator(sibling);
        }
      } catch {
        cleanupFailure = "device_apply_vault_failed";
      }
    }
    return cleanupFailure;
  }
  async #recoverPrepared(operation) {
    const blocked = { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    const target = contentTargetOf(operation);
    if (target !== null) {
      const cleanupFailure = await this.#cleanSiblingsOf(operation);
      const targetBytes = await this.#seam.readBytes(target);
      if (operation.operation === "updated") {
        if (await hashesTo2(targetBytes, operation.baseFingerprint)) {
          return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure };
        }
      }
      if (await hashesTo2(targetBytes, operation.finalFingerprint)) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: operation.finalFingerprint,
          rollbackToken: operation.tempToken,
          cleanupFailure
        };
      }
      if (operation.operation !== "updated" && targetBytes === null) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure };
      }
      return blocked;
    }
    if (operation.operation === "deleted") {
      const priorLocator2 = operation.priorLocator ?? "";
      const priorBytes = await this.#seam.readBytes(priorLocator2);
      if (priorBytes === null) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: null,
          rollbackToken: null,
          cleanupFailure: null
        };
      }
      if (operation.baseFingerprint === null || await hashesTo2(priorBytes, operation.baseFingerprint)) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure: null };
      }
      return blocked;
    }
    const priorLocator = operation.priorLocator ?? "";
    const targetLocator = operation.targetLocator ?? "";
    const priorExists = await this.#seam.locatorExists(priorLocator);
    const targetExists = await this.#seam.locatorExists(targetLocator);
    if (!priorExists && targetExists) {
      if (await hashesTo2(await this.#seam.readBytes(targetLocator), operation.finalFingerprint)) {
        return {
          kind: "mutated",
          eventSequence: operation.eventSequence,
          verifiedFingerprint: operation.finalFingerprint,
          rollbackToken: null,
          cleanupFailure: null
        };
      }
      return blocked;
    }
    if (priorExists && !targetExists) {
      if (await hashesTo2(await this.#seam.readBytes(priorLocator), operation.finalFingerprint)) {
        return { kind: "clean", eventSequence: operation.eventSequence, cleanupFailure: null };
      }
      return blocked;
    }
    return blocked;
  }
  async #recoverTempVerified(operation) {
    if (operation.operation !== "created" && operation.operation !== "updated") {
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    const target = contentTargetOf(operation);
    if (target === null || operation.tempToken === null) {
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    const tempLocator = buildTempSiblingLocator(target, operation.tempToken);
    const rollbackLocator = buildRollbackSiblingLocator(target, operation.tempToken);
    const tempExists = await this.#seam.locatorExists(tempLocator);
    const targetBytes = await this.#seam.readBytes(target);
    if (targetBytes !== null && await hashesTo2(targetBytes, operation.finalFingerprint)) {
      return {
        kind: "mutated",
        eventSequence: operation.eventSequence,
        verifiedFingerprint: operation.finalFingerprint,
        rollbackToken: operation.tempToken,
        cleanupFailure: await this.#cleanSiblingsOf(operation)
      };
    }
    if (targetBytes !== null && await hashesTo2(targetBytes, operation.baseFingerprint)) {
      if (!tempExists) {
        return {
          kind: "restored",
          eventSequence: operation.eventSequence,
          reason: "device_apply_vault_failed",
          cleanupFailure: null
        };
      }
      return this.#resumeReplace(operation, target, tempLocator, rollbackLocator);
    }
    if (targetBytes !== null) {
      if (await this.#seam.locatorExists(rollbackLocator)) {
        if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
          return {
            kind: "restored",
            eventSequence: operation.eventSequence,
            reason: "device_apply_vault_failed",
            cleanupFailure: null
          };
        }
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    if (tempExists) {
      return this.#resumeReplace(operation, target, tempLocator, rollbackLocator);
    }
    if (await this.#seam.locatorExists(rollbackLocator)) {
      if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
        return {
          kind: "restored",
          eventSequence: operation.eventSequence,
          reason: "device_apply_vault_failed",
          cleanupFailure: null
        };
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    if (operation.operation === "created") {
      return {
        kind: "restored",
        eventSequence: operation.eventSequence,
        reason: "device_apply_vault_failed",
        cleanupFailure: null
      };
    }
    return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
  }
  /** Resume the narrow replace from the durably verified staging bytes. */
  async #resumeReplace(operation, target, tempLocator, rollbackLocator) {
    const isUpdate = operation.operation === "updated";
    try {
      if (isUpdate && await this.#seam.locatorExists(target)) {
        await this.#seam.renameLocator(target, rollbackLocator);
      }
      await this.#seam.renameLocator(tempLocator, target);
    } catch (error) {
      throw this.#wrap(error, "recovery", "device_apply_vault_failed", true);
    }
    const finalBytes = await this.#seam.readBytes(target);
    if (!await hashesTo2(finalBytes, operation.finalFingerprint)) {
      if (isUpdate && await this.#seam.locatorExists(rollbackLocator)) {
        if (await this.#restoreRollback(target, rollbackLocator, operation.baseFingerprint)) {
          return {
            kind: "restored",
            eventSequence: operation.eventSequence,
            reason: "device_apply_vault_failed",
            cleanupFailure: null
          };
        }
      }
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    return {
      kind: "mutated",
      eventSequence: operation.eventSequence,
      verifiedFingerprint: operation.finalFingerprint,
      rollbackToken: operation.tempToken,
      cleanupFailure: await this.#cleanSiblingsOf(operation)
    };
  }
  async #recoverVaultMutated(operation) {
    const cleanupFailure = await this.#cleanSiblingsOf(operation);
    if (operation.operation === "deleted") {
      return {
        kind: "mutated",
        eventSequence: operation.eventSequence,
        verifiedFingerprint: null,
        rollbackToken: null,
        cleanupFailure
      };
    }
    const proofLocator = operation.operation === "renamed" || operation.operation === "moved" ? operation.targetLocator : contentTargetOf(operation);
    const proofBytes = proofLocator === null ? null : await this.#seam.readBytes(proofLocator);
    if (!await hashesTo2(proofBytes, operation.finalFingerprint)) {
      return { kind: "blocked", reason: "device_apply_recovery_ambiguous" };
    }
    return {
      kind: "mutated",
      eventSequence: operation.eventSequence,
      verifiedFingerprint: operation.finalFingerprint,
      rollbackToken: operation.tempToken,
      cleanupFailure
    };
  }
};

// src/device-sync/api.ts
var MANIFEST_RUN_STATES = [
  "collecting",
  "planned",
  "applying",
  "completed",
  "expired",
  "failed"
];
var DEVICE_SYNC_SERVER_REASON_SET = new Set(DEVICE_SYNC_SERVER_REASONS);
var DEVICE_EVENT_OPERATION_SET = new Set(DEVICE_SYNC_EVENT_OPERATIONS);
var MANIFEST_ACTION_KIND_SET = new Set(MANIFEST_ACTION_KINDS);
var MANIFEST_ACTION_REASON_SET = new Set(DEVICE_SYNC_ACTION_REASONS);
var MANIFEST_RUN_STATE_SET = new Set(MANIFEST_RUN_STATES);
var DeviceSyncApiError = class extends Error {
  reason;
  retryable;
  requestId;
  wireErrorCode;
  constructor(reason, retryable, requestId = null, wireErrorCode = null) {
    super(`device sync api failed: ${reason}`);
    this.name = "DeviceSyncApiError";
    this.reason = reason;
    this.retryable = retryable;
    this.requestId = requestId;
    this.wireErrorCode = wireErrorCode;
  }
};
function deviceSyncApiError(reason, retryable, requestId = null, wireErrorCode = null) {
  return new DeviceSyncApiError(reason, retryable, requestId, wireErrorCode);
}
function classifyDeviceSyncFailure(error) {
  if (error instanceof DeviceSyncApiError) {
    return {
      reason: error.reason,
      retryable: error.retryable,
      correlation: { requestId: error.requestId, wireErrorCode: error.wireErrorCode }
    };
  }
  if (error instanceof TypeError) {
    return { reason: "network_offline", retryable: true, correlation: void 0 };
  }
  return { reason: "server_error", retryable: true, correlation: void 0 };
}
var UUID_PATTERN14 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
var RFC_3339_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
var NON_NEGATIVE_INTEGER_TEXT_PATTERN2 = /^\d+$/;
var MAXIMUM_SAFE_INTEGER3 = 9007199254740991;
function parseEnvelopeRequestId3(value) {
  return typeof value === "string" && UUID_PATTERN14.test(value) ? value : null;
}
function parseEnvelope4(status, bodyText) {
  let parsed;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    throw mapWireFailure3(status, null, null);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw mapWireFailure3(status, null, null);
  }
  const envelope = parsed;
  const requestId = parseEnvelopeRequestId3(envelope.request_id);
  if (envelope.error !== null && envelope.error !== void 0) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : null;
    throw mapWireFailure3(status, code, requestId);
  }
  if (envelope.data === null || envelope.data === void 0) {
    throw mapWireFailure3(status, null, requestId);
  }
  return { data: envelope.data, requestId };
}
function mapWireFailure3(status, code, requestId) {
  if (code !== null && DEVICE_SYNC_SERVER_REASON_SET.has(code)) {
    const reason = code;
    return deviceSyncApiError(
      reason,
      reason === "device_sync_dependency_unavailable",
      requestId,
      code
    );
  }
  if (status === 401) {
    return deviceSyncApiError("access_expired", false, requestId, code);
  }
  if (status === 403) {
    return deviceSyncApiError(
      code === null ? "server_error" : "login_required",
      code === null,
      requestId,
      code
    );
  }
  if (status === 429) {
    return deviceSyncApiError("network_rate_limited", true, requestId, code);
  }
  return deviceSyncApiError("server_error", true, requestId, code);
}
function malformed2() {
  return deviceSyncApiError("server_error", true);
}
function isRecord6(value) {
  return typeof value === "object" && value !== null;
}
function isNonNegativeInteger6(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAXIMUM_SAFE_INTEGER3;
}
function requireNonNegativeInteger(value) {
  if (!isNonNegativeInteger6(value)) {
    throw malformed2();
  }
  return value;
}
function requireUuid2(value) {
  if (typeof value !== "string" || !UUID_PATTERN14.test(value)) {
    throw malformed2();
  }
  return value;
}
function requireTimestamp(value) {
  if (typeof value !== "string" || !RFC_3339_TIMESTAMP_PATTERN.test(value)) {
    throw malformed2();
  }
  return value;
}
function optionalUuid(value) {
  return value === null || value === void 0 ? null : requireUuid2(value);
}
function optionalText(value) {
  if (value === null || value === void 0) {
    return null;
  }
  if (typeof value !== "string") {
    throw malformed2();
  }
  return value;
}
function requireClosedMember(value, members) {
  if (typeof value !== "string" || !members.has(value)) {
    throw malformed2();
  }
  return value;
}
function parseWireFingerprint(value) {
  if (value === null || value === void 0) {
    return null;
  }
  if (!isRecord6(value)) {
    throw malformed2();
  }
  const sha256 = value["sha256"];
  const sizeBytes = value["size_bytes"];
  const mediaType = value["media_type"];
  if (typeof sha256 !== "string" || !FROZEN_FINGERPRINT_SHA256_PATTERN.test(sha256) || !isNonNegativeInteger6(sizeBytes) || typeof mediaType !== "string" || !isCanonicalMediaType(mediaType)) {
    throw malformed2();
  }
  return { sha256, sizeBytes, mediaType };
}
function parseDeviceSyncEvent(value) {
  if (!isRecord6(value)) {
    throw malformed2();
  }
  return {
    eventId: requireUuid2(value["event_id"]),
    eventSequence: requireNonNegativeInteger(value["event_sequence"]),
    operation: requireClosedMember(value["event_type"], DEVICE_EVENT_OPERATION_SET),
    sourceId: requireUuid2(value["source_id"]),
    originDeviceId: optionalUuid(value["origin_device_id"]),
    baseVersionId: optionalUuid(value["base_version_id"]),
    currentVersionId: optionalUuid(value["current_version_id"]),
    baseFingerprint: parseWireFingerprint(value["base_fingerprint"]),
    currentFingerprint: parseWireFingerprint(value["current_fingerprint"]),
    priorLocator: optionalText(value["prior_locator"]),
    resultingLocator: optionalText(value["resulting_locator"]),
    tombstoneId: optionalUuid(value["tombstone_id"]),
    committedAt: requireTimestamp(value["committed_at"])
  };
}
function parseDeviceEventPage(data) {
  if (!isRecord6(data)) {
    throw malformed2();
  }
  const events = data["events"];
  if (!Array.isArray(events)) {
    throw malformed2();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw malformed2();
  }
  return {
    acknowledgedSequence: requireNonNegativeInteger(data["acknowledged_sequence"]),
    deliveredThroughSequence: requireNonNegativeInteger(data["delivered_through_sequence"]),
    pageCheckpointSequence: requireNonNegativeInteger(data["page_checkpoint_sequence"]),
    events: events.map(parseDeviceSyncEvent),
    hasMore
  };
}
function parseCursorReceipt(data) {
  if (!isRecord6(data)) {
    throw malformed2();
  }
  return {
    acknowledgedSequence: requireNonNegativeInteger(data["acknowledged_sequence"]),
    deliveredThroughSequence: requireNonNegativeInteger(data["delivered_through_sequence"])
  };
}
function parseManifestRunReceipt(data) {
  if (!isRecord6(data)) {
    throw malformed2();
  }
  return {
    manifestRunId: requireUuid2(data["manifest_run_id"]),
    state: requireClosedMember(data["state"], MANIFEST_RUN_STATE_SET),
    baseAcknowledgedSequence: requireNonNegativeInteger(data["base_acknowledged_sequence"]),
    checkpointSequence: requireNonNegativeInteger(data["checkpoint_sequence"]),
    policyRevisionNumber: requireNonNegativeInteger(data["policy_revision_number"]),
    clientObservationGeneration: requireNonNegativeInteger(data["client_observation_generation"]),
    nextPageNumber: requireNonNegativeInteger(data["next_page_number"]),
    entryCount: requireNonNegativeInteger(data["entry_count"]),
    expiresAt: requireTimestamp(data["expires_at"])
  };
}
function parseManifestPageReceipt(data) {
  if (!isRecord6(data)) {
    throw malformed2();
  }
  return {
    manifestRunId: requireUuid2(data["manifest_run_id"]),
    pageNumber: requireNonNegativeInteger(data["page_number"]),
    acceptedEntryCount: requireNonNegativeInteger(data["accepted_entry_count"]),
    nextPageNumber: requireNonNegativeInteger(data["next_page_number"])
  };
}
function parseManifestAction(value) {
  if (!isRecord6(value)) {
    throw malformed2();
  }
  return {
    actionIndex: requireNonNegativeInteger(value["action_index"]),
    actionKind: requireClosedMember(value["action_kind"], MANIFEST_ACTION_KIND_SET),
    localEntryId: optionalText(value["local_entry_id"]),
    sourceId: optionalUuid(value["source_id"]),
    sourceVersionId: optionalUuid(value["source_version_id"]),
    sourceLocatorId: optionalUuid(value["source_locator_id"]),
    sourceTombstoneId: optionalUuid(value["source_tombstone_id"]),
    reason: value["reason"] === null || value["reason"] === void 0 ? null : requireClosedMember(value["reason"], MANIFEST_ACTION_REASON_SET),
    // The download placement locator parses with the same strictness as an
    // event locator: a string or nothing, never any other shape.
    checkpointLocator: optionalText(value["checkpoint_locator"])
  };
}
function parseManifestActionPage(data) {
  if (!isRecord6(data)) {
    throw malformed2();
  }
  const actions = data["actions"];
  if (!Array.isArray(actions)) {
    throw malformed2();
  }
  const hasMore = data["has_more"];
  if (typeof hasMore !== "boolean") {
    throw malformed2();
  }
  return {
    manifestRunId: requireUuid2(data["manifest_run_id"]),
    actions: actions.map(parseManifestAction),
    hasMore
  };
}
function createDeviceSyncApi(options) {
  const { transport, resolveOrigin, getAccessToken, refreshAccessToken, diagnostics } = options;
  function requireAccessToken() {
    const accessToken = getAccessToken();
    if (accessToken === null || accessToken.length === 0) {
      diagnostics.credentialFailure("access_missing", "login_required");
      throw deviceSyncApiError("login_required", false, null, null);
    }
    return accessToken;
  }
  function report(lane, reason, correlation) {
    if (lane.kind === "cursor") {
      diagnostics.cursorFailure(lane.stage, reason, correlation);
      return;
    }
    if (lane.kind === "reconcile") {
      diagnostics.reconcileFailure(lane.stage, reason, correlation);
      return;
    }
    diagnostics.applyFailure(lane.stage, reason, correlation);
  }
  function transportFailure(error) {
    const name = error instanceof Error ? error.name : "";
    if (name === "TimeoutError" || name === "AbortError") {
      return deviceSyncApiError("network_timeout", true);
    }
    return deviceSyncApiError("network_offline", true);
  }
  async function send(request) {
    try {
      return await transport(request);
    } catch (error) {
      throw transportFailure(error);
    }
  }
  async function run(lane, execute) {
    const accessToken = requireAccessToken();
    try {
      return await execute(accessToken);
    } catch (error) {
      let failure = error instanceof DeviceSyncApiError ? error : malformed2();
      if (failure.reason === "access_expired" && refreshAccessToken !== void 0) {
        try {
          await refreshAccessToken();
          const rotatedToken = requireAccessToken();
          return await execute(rotatedToken);
        } catch (retryError) {
          if (retryError instanceof DeviceSyncApiError) {
            failure = retryError;
          }
        }
      }
      report(lane, failure.reason, { requestId: failure.requestId, wireErrorCode: failure.wireErrorCode });
      throw failure;
    }
  }
  async function performJson(accessToken, request) {
    const response = await send({
      ...request,
      headers: { ...request.headers, authorization: `Bearer ${accessToken}` }
    });
    return parseEnvelope4(response.status, response.bodyText);
  }
  function jsonRequest(url, method, body) {
    return {
      url,
      method,
      headers: {
        accept: "application/json",
        // The live Desktop gate proved the real server rejects a JSON body
        // without an explicit content type (422 before any handler ran):
        // every carrying request names its media type exactly like the
        // journal lane's preflight does.
        ...body === void 0 ? {} : { "content-type": "application/json" }
      },
      ...body === void 0 ? {} : { body }
    };
  }
  return {
    async pullEvents() {
      return run({ kind: "cursor", stage: "pull" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/events`, "GET")
        );
        return parseDeviceEventPage(data);
      });
    },
    async acknowledgeCursor(input) {
      return run({ kind: "cursor", stage: "acknowledge" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/cursor-acknowledgements`, "POST", JSON.stringify({
            expected_previous_sequence: input.expectedPreviousSequence,
            applied_through_sequence: input.appliedThroughSequence
          }))
        );
        return parseCursorReceipt(data);
      });
    },
    async startManifest(input) {
      return run({ kind: "reconcile", stage: "start" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(`${resolveOrigin()}/api/sync/manifests`, "POST", JSON.stringify({
            client_observation_generation: input.clientObservationGeneration
          }))
        );
        return parseManifestRunReceipt(data);
      });
    },
    async appendManifestPage(input) {
      return run({ kind: "reconcile", stage: "page" }, async (accessToken) => {
        const wireBody = JSON.stringify({
          entries: input.entries.map((entry) => ({
            local_entry_id: entry.localEntryId,
            normalized_locator: entry.normalizedLocator,
            fingerprint: {
              sha256: entry.fingerprint.sha256,
              size_bytes: entry.fingerprint.sizeBytes,
              media_type: entry.fingerprint.mediaType
            },
            observation_generation: entry.observationGeneration,
            ...entry.knownSourceId === null || entry.knownSourceId === void 0 ? {} : { known_source_id: entry.knownSourceId },
            ...entry.knownVersionId === null || entry.knownVersionId === void 0 ? {} : { known_version_id: entry.knownVersionId }
          })),
          page_digest: input.pageDigest
        });
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/pages/${input.pageNumber}`,
            "PUT",
            wireBody
          )
        );
        return parseManifestPageReceipt(data);
      });
    },
    async finalizeManifest(input) {
      return run({ kind: "reconcile", stage: "finalize" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/finalize`,
            "POST",
            JSON.stringify({ total_entry_count: input.totalEntryCount, final_digest: input.finalDigest })
          )
        );
        return parseManifestRunReceipt(data);
      });
    },
    async listManifestActions(input) {
      return run({ kind: "reconcile", stage: "actions" }, async (accessToken) => {
        const query = new URLSearchParams();
        if (input.afterActionIndex !== void 0) {
          query.set("after_action_index", String(input.afterActionIndex));
        }
        if (input.limit !== void 0) {
          query.set("limit", String(input.limit));
        }
        const suffix = query.size > 0 ? `?${query.toString()}` : "";
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/actions${suffix}`,
            "GET"
          )
        );
        return parseManifestActionPage(data);
      });
    },
    async completeManifest(input) {
      return run({ kind: "reconcile", stage: "complete" }, async (accessToken) => {
        const { data } = await performJson(
          accessToken,
          jsonRequest(
            `${resolveOrigin()}/api/sync/manifests/${encodeURIComponent(input.manifestRunId)}/complete`,
            "POST",
            JSON.stringify({ final_digest: input.finalDigest })
          )
        );
        return parseCursorReceipt(data);
      });
    },
    async downloadSourceVersion(input) {
      return run({ kind: "apply", stage: "download" }, async (accessToken) => {
        const response = await send({
          url: `${resolveOrigin()}/api/sources/${encodeURIComponent(input.sourceId)}/versions/${encodeURIComponent(input.sourceVersionId)}/content`,
          method: "GET",
          headers: { authorization: `Bearer ${accessToken}`, accept: "application/octet-stream" }
        });
        if (response.status !== 200) {
          const { requestId: requestId2 } = parseEnvelope4(response.status, response.bodyText);
          throw deviceSyncApiError("server_error", true, requestId2, null);
        }
        const headers = response.headers;
        const declaredSha256 = headers["x-content-sha256"];
        const declaredSizeText = headers["content-length"];
        const mediaType = headers["content-type"];
        const requestId = parseEnvelopeRequestId3(headers["x-request-id"]);
        const bodyBytes = response.bodyBytes;
        const declaredSize = typeof declaredSizeText === "string" && NON_NEGATIVE_INTEGER_TEXT_PATTERN2.test(declaredSizeText) ? Number.parseInt(declaredSizeText, 10) : null;
        if (bodyBytes === null || declaredSize === null || declaredSize > MAXIMUM_SAFE_INTEGER3 || typeof declaredSha256 !== "string" || !FROZEN_FINGERPRINT_SHA256_PATTERN.test(declaredSha256) || typeof mediaType !== "string" || !isCanonicalMediaType(mediaType)) {
          throw deviceSyncApiError("device_download_integrity_failed", false, requestId, null);
        }
        const bytes = new Uint8Array(bodyBytes);
        if (bytes.byteLength !== declaredSize || await sha256Hex(bytes) !== declaredSha256) {
          throw deviceSyncApiError("device_download_integrity_failed", false, requestId, null);
        }
        return { bytes, declaredSha256, sizeBytes: declaredSize, mediaType };
      });
    }
  };
}

// src/device-sync/diagnostics.ts
var REGISTERED_SERVER_ERROR_CODE_SET = /* @__PURE__ */ new Set([
  ...DEVICE_SYNC_SERVER_REASONS,
  ...SYNC_API_ENVELOPE_ERROR_CODES
]);
function isRegisteredServerErrorCode(value) {
  return REGISTERED_SERVER_ERROR_CODE_SET.has(value);
}
function buildFailureTokens(stage, reason, correlation) {
  const tokens = [stage, reason];
  if (correlation !== void 0) {
    if (correlation.wireErrorCode !== null && isRegisteredServerErrorCode(correlation.wireErrorCode)) {
      tokens.push(correlation.wireErrorCode);
    }
    if (correlation.requestId !== null) {
      const requestIdToken = envelopeRequestId(correlation.requestId);
      if (requestIdToken !== null) {
        tokens.push(requestIdToken);
      }
    }
  }
  return tokens;
}
function appendObservation(trail, kind, tokens) {
  try {
    void trail.append({ kind, tokens }).catch(() => void 0);
  } catch {
  }
}
function createDeviceSyncDiagnostics(trail) {
  return {
    cursorFailure(stage, reason, correlation) {
      appendObservation(trail, "cursor_failure", buildFailureTokens(stage, reason, correlation));
    },
    applyFailure(stage, reason, correlation) {
      appendObservation(trail, "apply_failure", buildFailureTokens(stage, reason, correlation));
    },
    reconcileFailure(stage, reason, correlation) {
      appendObservation(
        trail,
        "reconcile_failure",
        buildFailureTokens(stage, reason, correlation)
      );
    },
    credentialFailure(stage, reason) {
      appendObservation(trail, "credential_failure", [stage, reason]);
    }
  };
}

// src/device-sync/echo-suppression.ts
function sqlText7(value) {
  return `'${value.replace(/'/g, "''")}'`;
}
var CONTENT_MARKER_OPERATIONS = /* @__PURE__ */ new Set(["created", "updated", "restored"]);
var RENAME_MARKER_OPERATIONS = /* @__PURE__ */ new Set(["renamed", "moved"]);
function createEchoSuppressor(options) {
  const { repository, database } = options;
  function readMarkersByLocator(read, locator) {
    const rows = read(
      [
        `select ${ECHO_MARKER_COLUMNS.join(", ")} from echo_markers`,
        `where prior_locator = ${sqlText7(locator)} or target_locator = ${sqlText7(locator)}`,
        "order by event_sequence asc;"
      ].join(" ")
    );
    const markers = [];
    for (const row of rows[0]?.values ?? []) {
      markers.push(parseEchoMarkerRow(row));
    }
    return markers;
  }
  async function consumeFirstExact(candidates, buildObservation) {
    for (const marker of candidates) {
      if (await repository.matchAndConsumeEcho(buildObservation(marker))) {
        return true;
      }
    }
    return false;
  }
  function consumeRenameObservationInSession(session, observation) {
    const candidates = readMarkersByLocator(
      (sql) => session.readRows(sql),
      observation.priorLocator
    ).filter(
      (marker) => RENAME_MARKER_OPERATIONS.has(marker.operation) && marker.targetLocator === observation.targetLocator
    );
    for (const marker of candidates) {
      const candidateObservation = {
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: marker.operation,
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: observation.fingerprint
      };
      if (isExactEchoMatch2(marker, candidateObservation)) {
        session.exec(`delete from echo_markers where event_sequence = ${marker.eventSequence};`);
        return true;
      }
    }
    return false;
  }
  return {
    async matchAndConsume(observation) {
      return repository.matchAndConsumeEcho(observation);
    },
    async consumeContentObservation(observation) {
      const candidates = readMarkersByLocator(
        (sql) => database.readAll(sql),
        observation.normalizedLocator
      ).filter((marker) => CONTENT_MARKER_OPERATIONS.has(marker.operation));
      return consumeFirstExact(candidates, (marker) => ({
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: marker.operation,
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: observation.fingerprint
      }));
    },
    async consumeRenameObservation(observation) {
      return database.runSerializedMutation(
        (session) => consumeRenameObservationInSession(session, observation)
      );
    },
    consumeRenameObservationInSession,
    async consumeDeleteObservation(observation) {
      const candidates = readMarkersByLocator(
        (sql) => database.readAll(sql),
        observation.priorLocator
      ).filter((marker) => marker.operation === "deleted");
      return consumeFirstExact(candidates, (marker) => ({
        eventSequence: marker.eventSequence,
        sourceId: observation.sourceId,
        operation: "deleted",
        priorLocator: marker.priorLocator,
        targetLocator: marker.targetLocator,
        fingerprint: null
      }));
    }
  };
}
function isSameFingerprint2(left, right) {
  if (left === null || right === null) {
    return left === right;
  }
  return left.sha256 === right.sha256 && left.sizeBytes === right.sizeBytes && left.mediaType === right.mediaType;
}
function isExactEchoMatch2(marker, observation) {
  if (observation.sourceId === null || observation.sourceId !== marker.sourceId) {
    return false;
  }
  if (observation.operation === null || observation.operation !== marker.operation) {
    return false;
  }
  if (marker.priorLocator !== null && observation.priorLocator !== marker.priorLocator) {
    return false;
  }
  if (marker.targetLocator !== null && observation.targetLocator !== marker.targetLocator) {
    return false;
  }
  if (marker.finalFingerprint !== null && !isSameFingerprint2(observation.fingerprint, marker.finalFingerprint)) {
    return false;
  }
  return true;
}

// src/device-sync/manifest-capture.ts
var MANIFEST_PAGE_ENTRIES = 500;
var MAX_MANIFEST_TOTAL_ENTRIES = 1e5;
async function buildManifestLocalEntryId(normalizedLocator) {
  const digest = await sha256Hex(
    new TextEncoder().encode(`manifest-entry/v1:${normalizedLocator}`)
  );
  return `me1-${digest}`;
}
async function computeManifestPageDigest(pageNumber, entries) {
  const payload = {
    version: 1,
    page: pageNumber,
    entries: entries.map((entry) => ({
      id: entry.localEntryId,
      locator: entry.normalizedLocator,
      sha256: entry.fingerprint.sha256,
      size_bytes: entry.fingerprint.sizeBytes,
      media_type: entry.fingerprint.mediaType,
      generation: entry.observationGeneration,
      known_source_id: entry.knownSourceId,
      known_version_id: entry.knownVersionId
    }))
  };
  return sha256Hex(canonicalJsonBytes(payload));
}
async function computeManifestFinalDigest(pages) {
  const ordered = [...pages].sort((left, right) => left.pageNumber - right.pageNumber);
  const payload = {
    version: 1,
    pages: ordered.map((page) => ({
      page: page.pageNumber,
      entries: page.entryCount,
      digest: page.pageDigest
    }))
  };
  return sha256Hex(canonicalJsonBytes(payload));
}
function normalizeLocatorOrNull(path) {
  if (typeof path !== "string") {
    return null;
  }
  try {
    return normalizePolicyLocator(path);
  } catch {
    return null;
  }
}
function isPositiveInteger6(value) {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}
function createManifestCapture(options) {
  const vaultReader = options.vaultReader;
  const identityReader = options.identityReader;
  const entriesPerPage = options.entriesPerPage !== void 0 && isPositiveInteger6(options.entriesPerPage) ? options.entriesPerPage : MANIFEST_PAGE_ENTRIES;
  async function* capturePages(barrierGeneration) {
    const snapshotPaths = await vaultReader.listRegularFilePaths();
    const normalizedLocators = [
      ...new Set(
        snapshotPaths.map((path) => normalizeLocatorOrNull(path)).filter((locator) => locator !== null)
      )
    ].sort();
    if (normalizedLocators.length > MAX_MANIFEST_TOTAL_ENTRIES) {
      throw new DeviceSyncApiError("device_manifest_capture_failed", false, null, null);
    }
    let pageNumber = 0;
    let batch = [];
    for (const normalizedLocator of normalizedLocators) {
      const contentBytes = await vaultReader.readRegularFileBytes(normalizedLocator);
      if (contentBytes === null) {
        continue;
      }
      const fingerprint = await deriveFrozenFingerprint(contentBytes);
      const trackedFile = identityReader.readLocalFileByPath(normalizedLocator);
      batch.push({
        localEntryId: await buildManifestLocalEntryId(normalizedLocator),
        normalizedLocator,
        fingerprint,
        observationGeneration: barrierGeneration,
        knownSourceId: trackedFile?.sourceId ?? null,
        knownVersionId: trackedFile?.baseVersionId ?? null
      });
      if (batch.length >= entriesPerPage) {
        yield {
          pageNumber,
          entries: batch,
          pageDigest: await computeManifestPageDigest(pageNumber, batch)
        };
        pageNumber += 1;
        batch = [];
      }
    }
    if (batch.length > 0 || pageNumber === 0) {
      yield {
        pageNumber,
        entries: batch,
        pageDigest: await computeManifestPageDigest(pageNumber, batch)
      };
    }
  }
  return { capturePages };
}

// src/device-sync/manifest-reconciler.ts
var RECONCILE_BARRIER_REASONS = {
  onboarding: "device_manifest_state_invalid",
  sqlite_rebuilt: "journal_image_invalid",
  cursor_gap: "device_cursor_gap",
  history_compacted: "device_event_unavailable",
  unknown_event: "device_event_integrity_failed",
  local_invariant: "device_manifest_state_invalid",
  explicit_repair: "device_manifest_state_invalid",
  periodic: "device_manifest_state_invalid"
};
function createManifestReconcilerJournal(options) {
  const repository = options.repository;
  const capture = options.capture;
  return {
    completeDeviceSyncRepair: (input) => repository.completeDeviceSyncRepair(input),
    discardActiveManifestRun: () => repository.discardActiveManifestRun(),
    readManifestPageProgress: () => repository.readManifestPageProgress(),
    readManifestActionProgress: () => repository.readManifestActionProgress(),
    recheckManifestActionTarget: (input) => capture.recheckForRepair(input),
    admitRepairUpload: async (input) => {
      const admission = await capture.admitForRepair(input.normalizedLocator);
      if (admission === null) {
        return "already_current";
      }
      if (admission.outcome === "capture_refused") {
        return "refused";
      }
      return "recorded";
    }
  };
}
var RUN_RESTART_REASONS = /* @__PURE__ */ new Set([
  "device_manifest_expired",
  "device_manifest_policy_advanced",
  "device_manifest_digest_mismatch"
]);
var RUN_EVIDENCE_INVALIDATION_REASONS = /* @__PURE__ */ new Set([
  "device_manifest_page_replay_mismatch",
  "device_manifest_digest_mismatch"
]);
var SYNTHETIC_EVENT_COMMITTED_AT = "1970-01-01T00:00:00Z";
var CLOSED_JOURNAL_STORE_ERROR_REASONS = new Set(
  JOURNAL_STORE_ERROR_REASONS
);
function storeReasonOf3(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string" && CLOSED_JOURNAL_STORE_ERROR_REASONS.has(reason)) {
      return reason;
    }
  }
  return null;
}
function closedFailureOf(error) {
  if (error instanceof DeviceSyncApiError) {
    return {
      reason: error.reason,
      retryable: error.retryable,
      requestId: error.requestId,
      wireErrorCode: error.wireErrorCode
    };
  }
  const storeReason = storeReasonOf3(error);
  if (storeReason !== null) {
    return { reason: storeReason, retryable: false, requestId: null, wireErrorCode: null };
  }
  const failure = classifyDeviceSyncFailure(error);
  return {
    reason: failure.reason,
    retryable: failure.retryable,
    requestId: failure.correlation?.requestId ?? null,
    wireErrorCode: failure.correlation?.wireErrorCode ?? null
  };
}
async function deterministicActionEventId(manifestRunId, actionIndex) {
  const digest = await sha256Hex(
    new TextEncoder().encode(`manifest-action-event/v1:${manifestRunId}:${actionIndex}`)
  );
  const versionNibble = "4";
  const variantNibble = (Number.parseInt(digest[16] ?? "0", 16) & 3 | 8).toString(16);
  return [
    digest.slice(0, 8),
    digest.slice(8, 12),
    `${versionNibble}${digest.slice(13, 16)}`,
    `${variantNibble}${digest.slice(17, 20)}`,
    digest.slice(20, 32)
  ].join("-");
}
function createManifestReconciler(options) {
  const { repository, api, capture, journal, applier, diagnostics } = options;
  const downloader = options.downloader;
  const actionPageLimit = options.actionPageLimit ?? 100;
  function runFailure(stage, error) {
    const failure = closedFailureOf(error);
    diagnostics.reconcileFailure(stage, failure.reason, {
      requestId: failure.requestId,
      wireErrorCode: failure.wireErrorCode
    });
    if (RUN_RESTART_REASONS.has(failure.reason)) {
      return { kind: "restart-run", stage, reason: failure.reason };
    }
    return failure.retryable ? { kind: "retry", reason: failure.reason } : { kind: "blocked", reason: failure.reason };
  }
  function blocked(stage, reason) {
    diagnostics.reconcileFailure(stage, reason);
    return { kind: "blocked", reason };
  }
  function restartRun(stage, reason) {
    diagnostics.reconcileFailure(stage, reason);
    return { kind: "restart-run", stage, reason };
  }
  function asOutcome(step) {
    if (step.kind === "restart-run" || step.kind === "stale-checkpoint") {
      return { kind: "blocked", reason: step.reason };
    }
    return step;
  }
  function wireEntries(entries) {
    return entries.map((entry) => ({
      localEntryId: entry.localEntryId,
      normalizedLocator: entry.normalizedLocator,
      fingerprint: entry.fingerprint,
      observationGeneration: entry.observationGeneration,
      ...entry.knownSourceId === null ? {} : { knownSourceId: entry.knownSourceId },
      ...entry.knownVersionId === null ? {} : { knownVersionId: entry.knownVersionId }
    }));
  }
  async function applyAction(manifestRunId, checkpointSequence, action, entriesByLocalId, terminalActionIndexes, attemptedActionIndexes, isRetryAttempt) {
    if (terminalActionIndexes.has(action.actionIndex)) {
      return null;
    }
    try {
      await repository.recordManifestAction({
        manifestRunId,
        actionIndex: action.actionIndex,
        actionKind: action.actionKind,
        outcome: "received",
        reason: action.reason
      });
    } catch (error) {
      return runFailure("actions", error);
    }
    const terminal = async (reason) => {
      try {
        await repository.recordManifestAction({
          manifestRunId,
          actionIndex: action.actionIndex,
          actionKind: action.actionKind,
          outcome: "terminal_safe",
          reason
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (reason !== null) {
        diagnostics.reconcileFailure("actions", reason);
      }
      terminalActionIndexes.add(action.actionIndex);
      return null;
    };
    const applySyntheticEvent = async (event2, options2) => {
      let settled;
      try {
        settled = await applier.apply(event2, options2);
      } catch (error) {
        const failure = closedFailureOf(error);
        if (failure.reason === "device_apply_vault_failed" && attemptedActionIndexes.has(action.actionIndex)) {
          try {
            settled = await applier.settleVaultFailedApply(event2, failure.reason);
          } catch (settleError) {
            return runFailure("actions", settleError);
          }
        } else {
          return runFailure("actions", error);
        }
      }
      return terminal(settled.outcome === "conflict" ? settled.reason : null);
    };
    if (action.actionKind === "conflict" || action.actionKind === "no_change" || action.actionKind === "excluded") {
      return terminal(action.reason);
    }
    const entry = action.localEntryId === null ? null : entriesByLocalId.get(action.localEntryId) ?? null;
    const canonicalOnlyLocator = entry === null && action.actionKind === "download" ? action.checkpointLocator : null;
    if (entry === null && canonicalOnlyLocator === null) {
      if (action.actionKind === "download") {
        return terminal("device_manifest_state_invalid");
      }
      return terminal("device_manifest_identity_ambiguous");
    }
    if (entry !== null) {
      let recheck;
      try {
        recheck = await journal.recheckManifestActionTarget({
          normalizedLocator: entry.normalizedLocator,
          entryFingerprint: entry.fingerprint,
          actionKind: action.actionKind === "upload" ? "upload" : action.actionKind === "apply_tombstone" ? "apply_tombstone" : "download"
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (recheck.kind === "blocked") {
        return terminal(recheck.reason);
      }
      if (action.actionKind === "upload") {
        let admission;
        try {
          admission = await journal.admitRepairUpload({
            normalizedLocator: entry.normalizedLocator
          });
        } catch (error) {
          return runFailure("actions", error);
        }
        if (admission === "refused") {
          return blocked("actions", "journal_mutation_failed");
        }
        return terminal(null);
      }
    }
    if (action.sourceId === null) {
      return terminal("device_manifest_identity_ambiguous");
    }
    let state;
    try {
      state = repository.readState();
    } catch (error) {
      return runFailure("actions", error);
    }
    const eventSequence = state.appliedSequence + 1;
    if (eventSequence > checkpointSequence) {
      if (state.barrierReason !== null && state.barrierReason !== "device_cursor_gap") {
        try {
          await repository.persistRepairBarrierReason("device_cursor_gap");
        } catch (error) {
          return runFailure("actions", error);
        }
      }
      if (isRetryAttempt && checkpointSequence <= state.appliedSequence) {
        return terminal("device_cursor_gap");
      }
      if (state.barrierReason !== "device_cursor_gap") {
        return blocked("actions", "device_cursor_gap");
      }
      diagnostics.reconcileFailure("actions", "device_cursor_gap");
      return { kind: "stale-checkpoint", stage: "actions", reason: "device_cursor_gap" };
    }
    let eventId;
    try {
      eventId = await deterministicActionEventId(manifestRunId, action.actionIndex);
    } catch (error) {
      return runFailure("actions", error);
    }
    let event;
    if (action.actionKind === "apply_tombstone") {
      if (entry === null) {
        return terminal("device_manifest_identity_ambiguous");
      }
      event = {
        eventId,
        eventSequence,
        operation: "deleted",
        sourceId: action.sourceId,
        originDeviceId: null,
        baseVersionId: null,
        currentVersionId: null,
        baseFingerprint: entry.fingerprint,
        currentFingerprint: null,
        priorLocator: entry.normalizedLocator,
        resultingLocator: null,
        tombstoneId: action.sourceTombstoneId,
        committedAt: SYNTHETIC_EVENT_COMMITTED_AT
      };
    } else {
      if (action.sourceVersionId === null) {
        return terminal("device_manifest_identity_ambiguous");
      }
      let verified;
      try {
        verified = await downloader({
          sourceId: action.sourceId,
          sourceVersionId: action.sourceVersionId
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      const verifiedFingerprint = {
        sha256: verified.declaredSha256,
        sizeBytes: verified.sizeBytes,
        mediaType: verified.mediaType
      };
      if (entry !== null) {
        event = {
          eventId,
          eventSequence,
          operation: "updated",
          sourceId: action.sourceId,
          originDeviceId: null,
          baseVersionId: entry.knownVersionId,
          currentVersionId: action.sourceVersionId,
          baseFingerprint: entry.fingerprint,
          currentFingerprint: verifiedFingerprint,
          // The wire's update shape: the resulting locator is the content
          // target and no prior locator exists (an update changes no path).
          priorLocator: null,
          resultingLocator: entry.normalizedLocator,
          tombstoneId: null,
          committedAt: SYNTHETIC_EVENT_COMMITTED_AT
        };
      } else {
        event = {
          eventId,
          eventSequence,
          operation: "created",
          sourceId: action.sourceId,
          originDeviceId: null,
          baseVersionId: null,
          currentVersionId: action.sourceVersionId,
          baseFingerprint: null,
          currentFingerprint: verifiedFingerprint,
          priorLocator: null,
          resultingLocator: canonicalOnlyLocator,
          tombstoneId: null,
          committedAt: SYNTHETIC_EVENT_COMMITTED_AT
        };
      }
      return await applySyntheticEvent(event, { verifiedDownload: verified });
    }
    return await applySyntheticEvent(event);
  }
  async function closeStaleCheckpoint(manifestRunId, checkpointSequence, finalDigest) {
    try {
      await journal.discardActiveManifestRun();
    } catch (error) {
      return runFailure("actions", error);
    }
    let cursorReceipt;
    try {
      cursorReceipt = await api.completeManifest({ manifestRunId, finalDigest });
    } catch (error) {
      return runFailure("complete", error);
    }
    if (cursorReceipt.acknowledgedSequence < checkpointSequence) {
      return blocked("complete", "device_manifest_state_invalid");
    }
    return { kind: "restart-run", stage: "actions", reason: "device_cursor_gap" };
  }
  async function runOne(barrierGeneration, isRetryAttempt) {
    let runReceipt;
    try {
      runReceipt = await api.startManifest({ clientObservationGeneration: barrierGeneration });
    } catch (error) {
      return runFailure("start", error);
    }
    const manifestRunId = runReceipt.manifestRunId;
    const checkpointSequence = runReceipt.checkpointSequence;
    if (runReceipt.clientObservationGeneration !== barrierGeneration) {
      return blocked("start", "device_manifest_state_invalid");
    }
    let boundState;
    try {
      boundState = repository.readState();
    } catch (error) {
      return runFailure("start", error);
    }
    if (boundState.activeManifestRunId !== null && boundState.activeManifestRunId !== manifestRunId || boundState.manifestCheckpointSequence !== null && boundState.manifestCheckpointSequence !== checkpointSequence) {
      try {
        await journal.discardActiveManifestRun();
      } catch (error) {
        return runFailure("start", error);
      }
      return { kind: "restart-run", stage: "start", reason: "device_manifest_state_invalid" };
    }
    let recordedPages;
    try {
      recordedPages = journal.readManifestPageProgress();
    } catch (error) {
      return runFailure("page", error);
    }
    const recordedByNumber = new Map(recordedPages.map((page) => [page.pageNumber, page]));
    const entriesByLocalId = /* @__PURE__ */ new Map();
    const pages = [];
    let lastPage = null;
    let serverNextPageNumber = runReceipt.nextPageNumber;
    try {
      for await (const page of capture.capturePages(barrierGeneration)) {
        for (const entry of page.entries) {
          entriesByLocalId.set(entry.localEntryId, entry);
        }
        const record = {
          pageNumber: page.pageNumber,
          entryCount: page.entries.length,
          pageDigest: page.pageDigest
        };
        const recorded = recordedByNumber.get(page.pageNumber);
        if (recorded !== void 0) {
          if (recorded.entryCount !== record.entryCount || recorded.pageDigest !== record.pageDigest) {
            return restartRun("page", "device_manifest_page_replay_mismatch");
          }
          pages.push(recorded);
          lastPage = recorded;
          continue;
        }
        if (page.pageNumber < serverNextPageNumber) {
          try {
            await repository.recordManifestPage({
              manifestRunId,
              pageNumber: record.pageNumber,
              entryCount: record.entryCount,
              pageDigest: record.pageDigest,
              checkpointSequence,
              finalDigest: null
            });
          } catch (error) {
            return runFailure("page", error);
          }
          pages.push(record);
          lastPage = record;
          continue;
        }
        let receipt;
        try {
          receipt = await api.appendManifestPage({
            manifestRunId,
            pageNumber: page.pageNumber,
            entries: wireEntries(page.entries),
            pageDigest: page.pageDigest
          });
        } catch (error) {
          return runFailure("page", error);
        }
        if (receipt.acceptedEntryCount !== page.entries.length || receipt.nextPageNumber !== page.pageNumber + 1 || receipt.manifestRunId !== manifestRunId) {
          return blocked("page", "device_manifest_state_invalid");
        }
        serverNextPageNumber = receipt.nextPageNumber;
        try {
          await repository.recordManifestPage({
            manifestRunId,
            pageNumber: record.pageNumber,
            entryCount: record.entryCount,
            pageDigest: record.pageDigest,
            checkpointSequence,
            finalDigest: null
          });
        } catch (error) {
          return runFailure("page", error);
        }
        pages.push(record);
        lastPage = record;
      }
    } catch (error) {
      return runFailure("page", error);
    }
    const totalEntryCount = pages.reduce((sum, page) => sum + page.entryCount, 0);
    let finalDigest;
    try {
      finalDigest = await computeManifestFinalDigest(pages);
    } catch (error) {
      return runFailure("page", error);
    }
    if (lastPage !== null) {
      try {
        await repository.recordManifestPage({
          manifestRunId,
          pageNumber: lastPage.pageNumber,
          entryCount: lastPage.entryCount,
          pageDigest: lastPage.pageDigest,
          checkpointSequence,
          finalDigest
        });
      } catch (error) {
        return runFailure("page", error);
      }
    }
    let plannedRun;
    try {
      plannedRun = await api.finalizeManifest({ manifestRunId, totalEntryCount, finalDigest });
    } catch (error) {
      return runFailure("finalize", error);
    }
    if (plannedRun.manifestRunId !== manifestRunId || plannedRun.entryCount !== totalEntryCount) {
      return blocked("finalize", "device_manifest_digest_mismatch");
    }
    let actionProgress;
    try {
      actionProgress = journal.readManifestActionProgress();
    } catch (error) {
      return runFailure("actions", error);
    }
    const terminalActionIndexes = new Set(
      actionProgress.filter((progress) => progress.outcome === "terminal_safe").map((progress) => progress.actionIndex)
    );
    const attemptedActionIndexes = new Set(
      actionProgress.filter((progress) => progress.outcome === "received").map((progress) => progress.actionIndex)
    );
    let afterActionIndex = void 0;
    let hasMoreActions = true;
    while (hasMoreActions) {
      let actionPage;
      try {
        actionPage = await api.listManifestActions({
          manifestRunId,
          ...afterActionIndex === void 0 ? {} : { afterActionIndex },
          limit: actionPageLimit
        });
      } catch (error) {
        return runFailure("actions", error);
      }
      if (actionPage.manifestRunId !== manifestRunId) {
        return blocked("actions", "device_manifest_state_invalid");
      }
      if (actionPage.actions.length === 0) {
        hasMoreActions = false;
        break;
      }
      for (const action of actionPage.actions) {
        const step = await applyAction(
          manifestRunId,
          checkpointSequence,
          action,
          entriesByLocalId,
          terminalActionIndexes,
          attemptedActionIndexes,
          isRetryAttempt
        );
        if (step !== null) {
          if (step.kind === "stale-checkpoint") {
            return closeStaleCheckpoint(manifestRunId, checkpointSequence, finalDigest);
          }
          return step;
        }
      }
      afterActionIndex = actionPage.actions[actionPage.actions.length - 1]?.actionIndex;
      hasMoreActions = actionPage.hasMore;
    }
    let cursorReceipt;
    try {
      cursorReceipt = await api.completeManifest({ manifestRunId, finalDigest });
    } catch (error) {
      return runFailure("complete", error);
    }
    if (cursorReceipt.acknowledgedSequence < checkpointSequence) {
      return blocked("complete", "device_manifest_state_invalid");
    }
    try {
      await journal.completeDeviceSyncRepair({
        manifestRunId,
        checkpointSequence,
        barrierGeneration
      });
    } catch (error) {
      return runFailure("complete", error);
    }
    return { kind: "completed", checkpointSequence };
  }
  async function runCheckpointBoundRuns(barrierGeneration) {
    let currentGeneration = barrierGeneration;
    for (let runAttempt = 0; runAttempt < 2; runAttempt += 1) {
      const step = await runOne(currentGeneration, runAttempt === 1);
      if (step.kind !== "restart-run") {
        return step;
      }
      if (runAttempt === 1) {
        return { kind: "blocked", reason: step.reason };
      }
      try {
        await journal.discardActiveManifestRun();
      } catch (error) {
        return asOutcome(runFailure(step.stage, error));
      }
      if (RUN_EVIDENCE_INVALIDATION_REASONS.has(step.reason)) {
        try {
          currentGeneration = await repository.advanceRepairBarrierGeneration(step.reason);
        } catch (error) {
          return asOutcome(runFailure(step.stage, error));
        }
      }
    }
    return { kind: "blocked", reason: "device_manifest_state_invalid" };
  }
  async function reconcile(reason) {
    let barrierGeneration;
    try {
      barrierGeneration = repository.readState().barrierGeneration;
    } catch (error) {
      return asOutcome(runFailure("start", error));
    }
    if (barrierGeneration === null) {
      try {
        barrierGeneration = await repository.nextObservationGeneration();
        await repository.startRepairBarrier({
          generation: barrierGeneration,
          reason: RECONCILE_BARRIER_REASONS[reason]
        });
      } catch (error) {
        return asOutcome(runFailure("start", error));
      }
    }
    return runCheckpointBoundRuns(barrierGeneration);
  }
  async function resume() {
    let barrierGeneration;
    try {
      barrierGeneration = repository.readState().barrierGeneration;
    } catch (error) {
      return asOutcome(runFailure("start", error));
    }
    if (barrierGeneration === null) {
      return asOutcome(blocked("start", "device_manifest_state_invalid"));
    }
    return runCheckpointBoundRuns(barrierGeneration);
  }
  return { reconcile, resume };
}

// src/device-sync/remote-event-applier.ts
function locatorsOf(event) {
  switch (event.operation) {
    case "created":
    case "restored":
      return { priorLocator: null, targetLocator: event.resultingLocator };
    case "updated":
      return { priorLocator: event.resultingLocator, targetLocator: null };
    case "renamed":
    case "moved":
      return { priorLocator: event.priorLocator, targetLocator: event.resultingLocator };
    case "deleted":
      return { priorLocator: event.priorLocator, targetLocator: null };
  }
}
function isContentOperation(operation) {
  return operation === "created" || operation === "updated" || operation === "restored";
}
function storeReasonOf4(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string") {
      return reason;
    }
  }
  return null;
}
function isConflictReason(reason) {
  return reason === "device_manifest_target_occupied" || reason === "device_manifest_local_diverged";
}
function createRemoteEventApplier(options) {
  const { repository, writer, downloader, diagnostics } = options;
  function closedStoreReasonOf(error) {
    const storeReason = storeReasonOf4(error);
    if (storeReason !== null) {
      try {
        const barrierReason = repository.readState().barrierReason;
        if (barrierReason !== null) {
          return barrierReason;
        }
      } catch {
      }
      return storeReason;
    }
    return "server_error";
  }
  function throwMapped(reason, retryable) {
    throw new DeviceSyncApiError(reason, retryable);
  }
  async function terminalize(eventSequence, outcome, reason, stage) {
    try {
      await repository.terminalizeEvent({ eventSequence, outcome, reason });
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure(stage, mapped);
      throwMapped(mapped, false);
    }
    return { eventSequence, outcome, reason };
  }
  async function apply(event, options2) {
    const state = repository.readState();
    if (event.eventSequence <= state.appliedSequence) {
      const settled = repository.readUnfinishedApply();
      if (settled !== null && settled.eventSequence === event.eventSequence) {
        if (settled.safeErrorCode !== null) {
          return {
            eventSequence: event.eventSequence,
            outcome: "conflict",
            reason: settled.safeErrorCode
          };
        }
        if (settled.operation === "deleted") {
          return { eventSequence: event.eventSequence, outcome: "tombstone_handled", reason: null };
        }
      }
      return { eventSequence: event.eventSequence, outcome: "applied", reason: null };
    }
    if (event.eventSequence !== state.appliedSequence + 1) {
      diagnostics.applyFailure("prepare", "device_cursor_gap");
      throwMapped("device_cursor_gap", false);
    }
    const locators = locatorsOf(event);
    const finalFingerprint = event.currentFingerprint;
    const baseFingerprint = event.baseFingerprint;
    const needsDownload = isContentOperation(event.operation);
    const contentTargetLocator = event.operation === "updated" ? locators.priorLocator : locators.targetLocator;
    const isLocatorOperation = event.operation === "renamed" || event.operation === "moved";
    const missingOperand = event.operation === "deleted" && locators.priorLocator === null || isLocatorOperation && (locators.priorLocator === null || locators.targetLocator === null) || needsDownload && (contentTargetLocator === null || event.currentVersionId === null || finalFingerprint === null) || isLocatorOperation && finalFingerprint === null && baseFingerprint === null;
    if (missingOperand) {
      diagnostics.applyFailure("prepare", "device_event_unavailable");
      throwMapped("device_event_unavailable", false);
    }
    const tempToken = needsDownload ? event.eventId : null;
    const prepared = {
      eventSequence: event.eventSequence,
      eventId: event.eventId,
      sourceId: event.sourceId,
      operation: event.operation,
      priorLocator: locators.priorLocator,
      targetLocator: locators.targetLocator,
      baseFingerprint,
      finalFingerprint: event.operation === "deleted" ? null : finalFingerprint,
      tempToken,
      rollbackToken: null
    };
    const marker = {
      eventSequence: event.eventSequence,
      sourceId: event.sourceId,
      operation: event.operation,
      priorLocator: locators.priorLocator,
      targetLocator: locators.targetLocator,
      finalFingerprint: event.operation === "deleted" ? null : finalFingerprint
    };
    try {
      await repository.prepareRemoteApply(prepared);
      await repository.recordEchoMarker(marker);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("prepare", mapped);
      throwMapped(mapped, false);
    }
    let bytes = null;
    if (needsDownload && event.currentVersionId !== null) {
      const predownloaded = options2?.verifiedDownload != null && options2.verifiedDownload.declaredSha256 === event.currentFingerprint?.sha256 ? options2.verifiedDownload : null;
      if (predownloaded !== null) {
        bytes = predownloaded.bytes;
      } else {
        try {
          const download = await downloader({
            sourceId: event.sourceId,
            sourceVersionId: event.currentVersionId
          });
          bytes = download.bytes;
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const failure = classifyDeviceSyncFailure(error);
          diagnostics.applyFailure("download", failure.reason, failure.correlation);
          throw new DeviceSyncApiError(
            failure.reason,
            failure.retryable,
            failure.correlation?.requestId ?? null,
            failure.correlation?.wireErrorCode ?? null
          );
        }
      }
    }
    const renameProof = finalFingerprint ?? baseFingerprint;
    try {
      if (event.operation === "deleted") {
        await writer.trash({
          eventSequence: event.eventSequence,
          priorLocator: locators.priorLocator ?? "",
          baseFingerprint
        });
      } else if (isLocatorOperation && renameProof !== null) {
        await writer.renameOrMove({
          eventSequence: event.eventSequence,
          operation: event.operation,
          priorLocator: locators.priorLocator ?? "",
          targetLocator: locators.targetLocator ?? "",
          expectedFinalFingerprint: renameProof
        });
      } else {
        if (contentTargetLocator === null || finalFingerprint === null || bytes === null) {
          diagnostics.applyFailure("prepare", "device_event_unavailable");
          throwMapped("device_event_unavailable", false);
        }
        await writer.stageAndReplace({
          eventSequence: event.eventSequence,
          operation: event.operation,
          targetLocator: contentTargetLocator,
          expectedFinalFingerprint: finalFingerprint,
          baseFingerprint: event.operation === "updated" ? baseFingerprint : null,
          bytes,
          tempToken: event.eventId
        });
      }
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        diagnostics.applyFailure(error.stage, error.reason);
        if (!error.retryable && (isConflictReason(error.reason) || error.restoredToBase)) {
          return terminalize(event.eventSequence, "conflict", error.reason, "local_commit");
        }
        throwMapped(error.reason, error.retryable);
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("vault_mutation", mapped);
      throwMapped(mapped, false);
    }
    try {
      await repository.transitionRemoteApply({
        eventSequence: event.eventSequence,
        state: "vault_mutated",
        rollbackToken: tempToken
      });
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("vault_mutation", mapped);
      throwMapped(mapped, false);
    }
    const mutatedRow = repository.readUnfinishedApply();
    if (mutatedRow !== null && mutatedRow.eventSequence === event.eventSequence) {
      try {
        const recovery = await writer.recover(mutatedRow);
        if (recovery.kind === "blocked") {
          diagnostics.applyFailure("recovery", recovery.reason);
        } else if (recovery.cleanupFailure !== null) {
          diagnostics.applyFailure("trash", recovery.cleanupFailure);
        }
      } catch (error) {
        const cleanupReason = error instanceof AtomicVaultWriterError ? error.reason : "device_apply_vault_failed";
        diagnostics.applyFailure("trash", cleanupReason);
      }
    }
    const outcome = event.operation === "deleted" ? "tombstone_handled" : "applied";
    return terminalize(event.eventSequence, outcome, null, "local_commit");
  }
  async function recoverUnfinishedApply() {
    const operation = repository.readUnfinishedApply();
    if (operation === null) {
      return;
    }
    let recovery;
    try {
      recovery = await writer.recover(operation);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        diagnostics.applyFailure(error.stage, error.reason);
        throwMapped(error.reason, error.retryable);
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("recovery", mapped);
      throwMapped(mapped, false);
    }
    if (recovery.kind !== "blocked" && recovery.cleanupFailure !== null) {
      diagnostics.applyFailure("trash", recovery.cleanupFailure);
    }
    switch (recovery.kind) {
      case "clean": {
        if (operation.state === "locally_applied" || operation.state === "server_acknowledged") {
          return;
        }
        try {
          await repository.abandonRemoteApply(operation.eventSequence);
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        if (repository.readState().activeManifestRunId !== null) {
          return;
        }
        try {
          const generation = await repository.nextObservationGeneration();
          await repository.startRepairBarrier({
            generation,
            reason: "device_apply_recovery_abandoned"
          });
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return;
      }
      case "mutated": {
        if (operation.state !== "vault_mutated" && operation.state !== "locally_applied" && operation.state !== "server_acknowledged") {
          try {
            await repository.transitionRemoteApply({
              eventSequence: operation.eventSequence,
              state: "vault_mutated",
              rollbackToken: recovery.rollbackToken
            });
          } catch (error) {
            if (error instanceof DeviceSyncApiError) {
              throw error;
            }
            const mapped = closedStoreReasonOf(error);
            diagnostics.applyFailure("recovery", mapped);
            throwMapped(mapped, false);
          }
        }
        const outcome = operation.operation === "deleted" ? "tombstone_handled" : "applied";
        await terminalize(operation.eventSequence, outcome, null, "recovery");
        return;
      }
      case "restored": {
        await terminalize(operation.eventSequence, "conflict", recovery.reason, "recovery");
        return;
      }
      case "blocked": {
        diagnostics.applyFailure("recovery", recovery.reason);
        try {
          const generation = await repository.nextObservationGeneration();
          await repository.startRepairBarrier({ generation, reason: recovery.reason });
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return;
      }
    }
  }
  async function settleVaultFailedApply(event, reason) {
    const leftover = repository.readRemoteApply(event.eventSequence);
    if (leftover === null) {
      return { eventSequence: event.eventSequence, outcome: "conflict", reason };
    }
    if (leftover.eventId !== event.eventId || leftover.state === "locally_applied" || leftover.state === "server_acknowledged") {
      diagnostics.applyFailure("recovery", "device_apply_recovery_ambiguous");
      throwMapped("device_apply_recovery_ambiguous", false);
    }
    let recovery;
    try {
      recovery = await writer.recover(leftover);
    } catch (error) {
      if (error instanceof DeviceSyncApiError) {
        throw error;
      }
      if (error instanceof AtomicVaultWriterError) {
        diagnostics.applyFailure(error.stage, error.reason);
        return terminalize(event.eventSequence, "conflict", error.reason, "recovery");
      }
      const mapped = closedStoreReasonOf(error);
      diagnostics.applyFailure("recovery", mapped);
      throwMapped(mapped, false);
    }
    if (recovery.kind !== "blocked" && recovery.cleanupFailure !== null) {
      diagnostics.applyFailure("trash", recovery.cleanupFailure);
    }
    switch (recovery.kind) {
      case "clean": {
        try {
          await repository.abandonRemoteApply(event.eventSequence);
        } catch (error) {
          if (error instanceof DeviceSyncApiError) {
            throw error;
          }
          const mapped = closedStoreReasonOf(error);
          diagnostics.applyFailure("recovery", mapped);
          throwMapped(mapped, false);
        }
        return { eventSequence: event.eventSequence, outcome: "conflict", reason };
      }
      case "mutated": {
        if (leftover.state !== "vault_mutated") {
          try {
            await repository.transitionRemoteApply({
              eventSequence: event.eventSequence,
              state: "vault_mutated",
              rollbackToken: recovery.rollbackToken
            });
          } catch (error) {
            if (error instanceof DeviceSyncApiError) {
              throw error;
            }
            const mapped = closedStoreReasonOf(error);
            diagnostics.applyFailure("recovery", mapped);
            throwMapped(mapped, false);
          }
        }
        const outcome = leftover.operation === "deleted" ? "tombstone_handled" : "applied";
        return terminalize(event.eventSequence, outcome, null, "recovery");
      }
      case "restored": {
        return terminalize(event.eventSequence, "conflict", recovery.reason, "recovery");
      }
      case "blocked": {
        diagnostics.applyFailure("recovery", recovery.reason);
        throwMapped(recovery.reason, false);
      }
    }
  }
  return { recoverUnfinishedApply, settleVaultFailedApply, apply };
}

// src/device-sync/sync-coordinator.ts
var DEVICE_SYNC_PULL_INTERVAL_MS = 3e4;
var DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS = 6 * 60 * 60 * 1e3;
var DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS = 60 * 60 * 1e3;
var DEVICE_SYNC_REPAIR_RETRY_BOUND = 3;
function createRealSyncScheduler() {
  return (delayMs, callback) => {
    const handle = setTimeout(callback, delayMs);
    return () => {
      clearTimeout(handle);
    };
  };
}
function storeReasonOf5(error) {
  if (error !== null && typeof error === "object" && "reason" in error) {
    const reason = error.reason;
    if (typeof reason === "string") {
      return reason;
    }
  }
  return null;
}
function classifyCycleFailure(error) {
  if (error instanceof DeviceSyncApiError) {
    return { reason: error.reason, retryable: error.retryable };
  }
  const failure = classifyDeviceSyncFailure(error);
  return { reason: failure.reason, retryable: failure.retryable };
}
function createSyncCoordinator(options) {
  const {
    repository,
    api,
    applier,
    reconciler,
    outbound,
    diagnostics,
    nowEpochMs
  } = options;
  const scheduler = options.scheduler ?? createRealSyncScheduler();
  const randomJitter = options.randomJitter ?? Math.random;
  let isStopped = false;
  let drainPromise = null;
  let hasPendingCycle = false;
  let hasPendingExplicitRepair = false;
  let hasPendingPeriodicReconcile = false;
  let hasFollowUpCycle = false;
  let isRepairRunning = false;
  let blockedRepairReason = null;
  let consecutiveRepairRetryCount = 0;
  let consecutiveRepairRetryReason = null;
  let failureAttemptCount = 0;
  let cadenceCanceller = null;
  let cadenceNextEpochMs = null;
  let retryCanceller = null;
  let accumulatedActiveMs = 0;
  let lastActivityEpochMs = null;
  let hasExpiredSuspension = false;
  function cancelCadenceTimer() {
    if (cadenceCanceller !== null) {
      cadenceCanceller();
      cadenceCanceller = null;
    }
    cadenceNextEpochMs = null;
  }
  function cancelRetryTimer() {
    if (retryCanceller !== null) {
      retryCanceller();
      retryCanceller = null;
    }
  }
  function armCadenceTimer() {
    if (isStopped || cadenceCanceller !== null || retryCanceller !== null) {
      return;
    }
    const nowEpoch = nowEpochMs();
    const fireAtEpochMs = cadenceNextEpochMs === null ? nowEpoch + DEVICE_SYNC_PULL_INTERVAL_MS : cadenceNextEpochMs + DEVICE_SYNC_PULL_INTERVAL_MS;
    cadenceNextEpochMs = fireAtEpochMs;
    cadenceCanceller = scheduler(Math.max(0, fireAtEpochMs - nowEpoch), () => {
      cadenceCanceller = null;
      accumulatedActiveMs += DEVICE_SYNC_PULL_INTERVAL_MS;
      if (accumulatedActiveMs >= DEVICE_SYNC_RECONCILE_ACCUMULATED_ACTIVE_MS) {
        accumulatedActiveMs = 0;
        hasPendingPeriodicReconcile = true;
      }
      requestSync("pull_interval");
      armCadenceTimer();
    });
  }
  function scheduleRetryBackoff() {
    failureAttemptCount += 1;
    cancelRetryTimer();
    cancelCadenceTimer();
    const delayMs = computeRetryBackoffMs(failureAttemptCount, randomJitter);
    retryCanceller = scheduler(delayMs, () => {
      retryCanceller = null;
      requestSync("pull_interval");
    });
  }
  function readState(stage) {
    try {
      return repository.readState();
    } catch (error) {
      const reason = storeReasonOf5(error) ?? "server_error";
      diagnostics.cursorFailure(stage, reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }
  async function acknowledgeOwedCursor() {
    const state = readState("acknowledge");
    if (state.appliedSequence <= state.acknowledgedSequence) {
      return;
    }
    const receipt = await api.acknowledgeCursor({
      expectedPreviousSequence: state.acknowledgedSequence,
      appliedThroughSequence: state.appliedSequence
    });
    const acknowledged = Math.min(receipt.acknowledgedSequence, state.appliedSequence);
    if (acknowledged <= state.acknowledgedSequence) {
      return;
    }
    try {
      await repository.recordServerAcknowledgement(acknowledged);
    } catch (error) {
      const reason = storeReasonOf5(error) ?? "server_error";
      diagnostics.cursorFailure("acknowledge", reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }
  function isProvenSelfOriginNoOp(event) {
    const resolveOwnDeviceId = options.resolveOwnDeviceId;
    const evidenceReader = options.outboundEvidence;
    if (resolveOwnDeviceId === void 0 || evidenceReader === void 0) {
      return false;
    }
    const ownDeviceId = resolveOwnDeviceId();
    if (ownDeviceId === null || ownDeviceId.length === 0 || event.originDeviceId === null || event.originDeviceId !== ownDeviceId) {
      return false;
    }
    const normalizedLocator = event.operation === "deleted" ? event.priorLocator : event.resultingLocator;
    if (normalizedLocator === null || event.currentVersionId === null || event.currentFingerprint === null) {
      return false;
    }
    const row = evidenceReader.readCommittedOutboundRowByLocator(normalizedLocator);
    if (row === null || row.sourceId !== event.sourceId || row.baseVersionId !== event.currentVersionId) {
      return false;
    }
    const committed = row.lastCommittedFingerprint;
    return committed !== null && committed.sha256 === event.currentFingerprint.sha256 && committed.sizeBytes === event.currentFingerprint.sizeBytes && committed.mediaType === event.currentFingerprint.mediaType;
  }
  async function terminalizeSelfOriginNoOp(event) {
    try {
      await repository.terminalizeEvent({
        eventSequence: event.eventSequence,
        outcome: "self_origin_no_op",
        reason: null
      });
    } catch (error) {
      const reason = storeReasonOf5(error) ?? "server_error";
      diagnostics.applyFailure("local_commit", reason);
      throw new DeviceSyncApiError(reason, false);
    }
  }
  async function runRepairIfRequired() {
    const state = readState("pull");
    const isRepairOwed = state.barrierGeneration !== null || state.activeManifestRunId !== null || (options.isJournalReconcileRequired?.() ?? false);
    if (!hasPendingExplicitRepair && (!isRepairOwed || blockedRepairReason !== null)) {
      hasPendingPeriodicReconcile = false;
      return "none";
    }
    const isExplicitRepair = hasPendingExplicitRepair;
    const isPeriodicReconcile = hasPendingPeriodicReconcile;
    hasPendingExplicitRepair = false;
    hasPendingPeriodicReconcile = false;
    const reconcileReason = isExplicitRepair ? "explicit_repair" : isPeriodicReconcile ? "periodic" : state.barrierGeneration === null && state.activeManifestRunId === null ? "local_invariant" : null;
    isRepairRunning = true;
    let outcome;
    try {
      outcome = reconcileReason === null ? await reconciler.resume() : await reconciler.reconcile(reconcileReason);
    } finally {
      isRepairRunning = false;
    }
    if (outcome.kind === "retry") {
      if (consecutiveRepairRetryReason === outcome.reason) {
        consecutiveRepairRetryCount += 1;
      } else {
        consecutiveRepairRetryReason = outcome.reason;
        consecutiveRepairRetryCount = 1;
      }
      if (consecutiveRepairRetryCount >= DEVICE_SYNC_REPAIR_RETRY_BOUND) {
        blockedRepairReason = outcome.reason;
        consecutiveRepairRetryCount = 0;
        consecutiveRepairRetryReason = null;
        return "settled";
      }
      return "retry";
    }
    if (outcome.kind === "blocked") {
      blockedRepairReason = outcome.reason;
      return "settled";
    }
    blockedRepairReason = null;
    consecutiveRepairRetryCount = 0;
    consecutiveRepairRetryReason = null;
    return "settled";
  }
  async function runCycle(trigger) {
    lastActivityEpochMs = nowEpochMs();
    let shouldArmFollowUp = false;
    try {
      if (hasExpiredSuspension) {
        hasExpiredSuspension = false;
        const suspendedState = readState("pull");
        if (suspendedState.activeManifestRunId !== null && options.discardExpiredManifestRun !== void 0) {
          await options.discardExpiredManifestRun();
        }
      }
      await applier.recoverUnfinishedApply();
      const repairOutcome = await runRepairIfRequired();
      if (repairOutcome === "retry") {
        scheduleRetryBackoff();
        return;
      }
      const postRepairState = readState("pull");
      if (postRepairState.barrierGeneration === null && postRepairState.activeManifestRunId === null) {
        await outbound.request();
      }
      await acknowledgeOwedCursor();
      const page = await api.pullEvents();
      for (const event of page.events) {
        const state = readState("pull");
        if (event.eventSequence <= state.appliedSequence) {
          continue;
        }
        if (isProvenSelfOriginNoOp(event)) {
          await terminalizeSelfOriginNoOp(event);
        } else {
          await applier.apply(event);
        }
      }
      await acknowledgeOwedCursor();
      shouldArmFollowUp = page.hasMore && !trigger.isFollowUp;
    } catch (error) {
      if (classifyCycleFailure(error).retryable) {
        scheduleRetryBackoff();
      }
      return;
    } finally {
      lastActivityEpochMs = nowEpochMs();
    }
    failureAttemptCount = 0;
    cancelRetryTimer();
    armCadenceTimer();
    if (shouldArmFollowUp) {
      hasFollowUpCycle = true;
    }
  }
  function startDrain() {
    if (drainPromise !== null || isStopped) {
      return;
    }
    const runningDrain = drain().catch(() => void 0);
    drainPromise = runningDrain;
    void runningDrain.finally(() => {
      if (drainPromise === runningDrain) {
        drainPromise = null;
      }
    });
  }
  async function drain() {
    while (!isStopped && (hasPendingCycle || hasFollowUpCycle)) {
      const isFollowUp = hasFollowUpCycle && !hasPendingCycle;
      hasFollowUpCycle = false;
      hasPendingCycle = false;
      await runCycle({ isFollowUp });
    }
  }
  function requestSync(trigger) {
    if (isStopped) {
      return;
    }
    const nowEpoch = nowEpochMs();
    if (lastActivityEpochMs !== null && nowEpoch - lastActivityEpochMs >= DEVICE_SYNC_MANIFEST_EXPIRY_AFTER_SUSPEND_MS) {
      hasExpiredSuspension = true;
    }
    if (trigger === "explicit_repair") {
      blockedRepairReason = null;
      consecutiveRepairRetryCount = 0;
      consecutiveRepairRetryReason = null;
      hasPendingExplicitRepair = true;
    }
    if (trigger === "periodic_reconcile") {
      hasPendingPeriodicReconcile = true;
    }
    hasPendingCycle = true;
    armCadenceTimer();
    startDrain();
  }
  return {
    request: requestSync,
    stop() {
      isStopped = true;
      cancelCadenceTimer();
      cancelRetryTimer();
      return drainPromise ?? Promise.resolve();
    },
    readStatus() {
      return projectDeviceSyncStatus({
        state: repository.readState(),
        isRepairRunning,
        blockedRepairReason,
        isJournalReconcileRequired: options.isJournalReconcileRequired?.() ?? false,
        manifestActions: options.readManifestActionProgress?.() ?? []
      });
    }
  };
}

// src/plugin.ts
var ALLOW_LOOPBACK_HTTP_ORIGIN = false;
var SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS = 250;
var MAX_BUFFERED_STARTUP_FAILURE_ENTRIES = 8;
var DEFAULT_DEVICE_NAME = "Obsidian vault";
var RESTORE_RESERVATION_REFUSAL_NOTICES = {
  restore_target_occupied: "Restore refused: the target path is already occupied. Choose another target.",
  restore_target_busy: "Restore postponed: an upload for the target path is in flight. Try again shortly.",
  restore_already_pending: "A restore for this tombstone is already in progress. Wait for it to finish."
};
var UUID_PATTERN15 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
var POLICY_CACHE_PLUGIN_DATA_KEY = "policy_cache";
var JOURNAL_ENGINE_WASM_FILE_NAME = "sql-wasm.wasm";
function createRequestUrlDeviceHttpTransport() {
  return async (request) => {
    const result = await (0, import_obsidian5.requestUrl)({
      url: request.url,
      method: request.method,
      headers: { ...request.headers },
      body: request.body,
      throw: false
    });
    return { status: result.status, bodyText: result.text };
  };
}
function resolvePlatformName() {
  if (import_obsidian5.Platform.isIosApp) {
    return "ios";
  }
  if (import_obsidian5.Platform.isAndroidApp) {
    return "android";
  }
  if (import_obsidian5.Platform.isWin) {
    return "windows";
  }
  if (import_obsidian5.Platform.isMacOS) {
    return "macos";
  }
  if (import_obsidian5.Platform.isLinux) {
    return "linux";
  }
  return import_obsidian5.Platform.isDesktop ? "desktop" : "mobile";
}
function resolveMultipartPlatformClass() {
  return import_obsidian5.Platform.isDesktop ? "desktop" : "mobile";
}
function normalizePendingGrant(value) {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const candidate = value;
  if (typeof candidate["grant_id"] !== "string" || typeof candidate["user_code"] !== "string" || typeof candidate["verification_uri"] !== "string" || typeof candidate["expires_at_epoch_seconds"] !== "number" || typeof candidate["poll_interval_seconds"] !== "number") {
    return null;
  }
  return {
    grant_id: candidate["grant_id"],
    user_code: candidate["user_code"],
    verification_uri: candidate["verification_uri"],
    expires_at_epoch_seconds: candidate["expires_at_epoch_seconds"],
    poll_interval_seconds: candidate["poll_interval_seconds"]
  };
}
function normalizeSettings(loaded) {
  const candidate = typeof loaded === "object" && loaded !== null ? loaded : {};
  const loadedRecordName = typeof candidate["secret_record_name"] === "string" && isSecretRecordNameValid(candidate["secret_record_name"]) ? candidate["secret_record_name"] : null;
  const loadedClientId = typeof candidate["client_instance_id"] === "string" && UUID_PATTERN15.test(candidate["client_instance_id"]) ? candidate["client_instance_id"] : null;
  const loadedServerDeviceId = typeof candidate["device_id"] === "string" && UUID_PATTERN15.test(candidate["device_id"]) ? candidate["device_id"] : null;
  return {
    server_origin: typeof candidate["server_origin"] === "string" ? candidate["server_origin"] : "",
    device_name: typeof candidate["device_name"] === "string" ? validateDeviceName(candidate["device_name"]) ?? DEFAULT_DEVICE_NAME : DEFAULT_DEVICE_NAME,
    client_instance_id: loadedClientId ?? crypto.randomUUID(),
    device_id: loadedServerDeviceId,
    // A valid stored record name round-trips unchanged (plugin hygiene,
    // 2026-08-16 §12): the earlier rewrite to the build-time constant
    // renamed every stored SecretStorage record on each load.
    secret_record_name: loadedRecordName,
    pending_grant: normalizePendingGrant(candidate["pending_grant"])
  };
}
var KnowledgeWorkspacePlugin = class extends import_obsidian5.Plugin {
  #settings = {
    server_origin: "",
    device_name: DEFAULT_DEVICE_NAME,
    client_instance_id: "",
    device_id: null,
    secret_record_name: null,
    pending_grant: null
  };
  #connectionState = "not_connected";
  #statusDetail = null;
  #controller = null;
  #session = null;
  #settingTab = null;
  #policySession = null;
  #policyState = "policy_not_initialized";
  #journalPersistence = null;
  #capture = null;
  #lifecycleCapture = null;
  #queueDriver = null;
  #queueRepository = null;
  /**
   * The durable diagnostics trail (sync error tracing task 1): retained on
   * the plugin so the settings snapshot and the copy-sync-diagnostics
   * export can read the tail, the counts and the derived stop reasons.
   */
  #diagnosticTrail = null;
  #journalFailureReporter = null;
  /**
   * Closed-reason surfacing C1 P1: the closed tokens of the last journal
   * startup failure (the failed stage token plus the closed store reason
   * when the throw was a store error), or null before the first failure —
   * never a fake success token. Feeds the settings snapshot and the
   * self-check's journal-not-running verdict.
   */
  #lastStartupFailureTokens = null;
  /**
   * Closed-reason surfacing C1 P4: startup-failure token lists recorded
   * before the trail sidecar is loaded; flushed into the trail right after
   * its load (bounded by MAX_BUFFERED_STARTUP_FAILURE_ENTRIES).
   */
  #bufferedStartupFailureTokenLists = [];
  /** C1 P5: has the pending-count read swallow already been recorded? */
  #hasRecordedStatusReadFailure = false;
  /** C1 P5: has the note-status read swallow already been recorded? */
  #hasRecordedNoteStatusReadFailure = false;
  #hasReportedRetryScheduleReadFailure = false;
  #hasReportedSyncStatusReadFailure = false;
  #automaticSnapshotCoordinator = null;
  #boundedQueuePassDispatcher = null;
  /**
   * The single device-sync coordinator (task 12): owns every mutating
   * foreground network phase of the device cursor and manifest
   * reconciliation stack. Null before the journal starts or after unload.
   */
  #syncCoordinator = null;
  #pendingAutomaticSnapshotReason = null;
  #isQueuePassActive = false;
  #lastQueuePassOutcome = null;
  #syncStatusBarItem = null;
  /** The one-shot scheduled retry trigger's outstanding timer (fix round 2 D4). */
  #scheduledRetryPassTimer = null;
  /** The deadline the outstanding timer fires at, or null when disarmed. */
  #scheduledRetryPassTargetEpochMs = null;
  /**
   * The composed Conflict Inbox controller (conflict inbox task 9): null
   * before the journal starts, after unload, and on a fail-closed journal
   * startup — the inbox command gates on exactly this fact.
   */
  #conflictController = null;
  /** The conflict composition's closed-token diagnostics sink (task 9). */
  #conflictDiagnostics = null;
  /** The durable no-byte conflict repair repository (task 9). */
  #conflictRepository = null;
  async onload() {
    this.#settings = normalizeSettings(await this.loadData());
    await this.#persistSettings();
    const secretStore = this.app.secretStorage;
    const transport = createDeviceApiTransport(
      createRequestUrlDeviceHttpTransport(),
      () => parseServerOrigin(this.#settings.server_origin, {
        allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
      }) ?? ""
    );
    const session = new DeviceTokenSession({
      transport,
      secretStore,
      recordName: DEVICE_CREDENTIAL_RECORD_NAME,
      settings: this.#settings,
      persistSettings: () => this.#persistSettings(),
      createRotationId: () => crypto.randomUUID(),
      onStateChange: (state, detail) => this.#setConnectionState(state, detail)
    });
    const policySession = new PolicySession({
      http: createObsidianPolicyHttpTransport(),
      resolveOrigin: () => parseServerOrigin(this.#settings.server_origin, {
        allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
      }) ?? "",
      getAccessToken: () => session.accessCredential,
      cache: this.#createPolicyCacheAdapter(),
      onStateChange: (state) => {
        this.#policyState = state;
      }
    });
    await policySession.restoreFromCache();
    this.#policyState = policySession.state;
    const controller = new DeviceAuthorizationController({
      transport,
      secretStore,
      recordName: DEVICE_CREDENTIAL_RECORD_NAME,
      settings: this.#settings,
      persistSettings: () => this.#persistSettings(),
      clientIdentity: {
        platformClass: import_obsidian5.Platform.isDesktop ? "obsidian_desktop" : "obsidian_mobile",
        platformName: resolvePlatformName(),
        pluginVersion: this.manifest.version,
        clientInstanceId: this.#settings.client_instance_id
      },
      allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN,
      openUrl: (url) => {
        window.open(url, "_blank");
      },
      delay: (milliseconds) => new Promise((resolve) => {
        window.setTimeout(resolve, milliseconds);
      }),
      nowEpochMs: () => Date.now(),
      onStateChange: (state, detail) => this.#setConnectionState(state, detail),
      onExchange: async (exchange) => {
        this.#settings.device_id = exchange.device_id;
        await this.#persistSettings();
        session.adoptExchange(exchange);
        await policySession.adoptOnboardingTrust();
        this.#requestAutomaticSnapshot("policy_accepted");
      }
    });
    this.#session = session;
    this.#controller = controller;
    this.#policySession = policySession;
    this.#settingTab = new DeviceAuthenticationSettingTab(this.app, this, {
      getSnapshot: () => {
        const syncStatus = this.#projectSyncStatus();
        const generationPublishFailures = this.#journalPersistence?.readGenerationPublishFailureSummary() ?? null;
        const trailEntries = this.#diagnosticTrail?.readEntries() ?? [];
        const secretRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
        return {
          connectionState: this.#connectionState,
          statusDetail: this.#statusDetail,
          // C2 A3: the closed ClearedReason of the terminal tombstone.
          clearedReason: secretRecord?.state === "cleared" ? secretRecord.cleared_reason : null,
          serverOrigin: this.#settings.server_origin,
          deviceName: this.#settings.device_name,
          hasPendingGrant: this.#settings.pending_grant !== null,
          hasActiveCredential: this.#resolveHasActiveCredential(secretStore),
          syncStatusText: syncStatus === null ? null : SYNC_STATUS_TEXT[syncStatus.kind],
          syncBlockerGuidance: syncStatus === null ? [] : [...syncBlockerGuidanceLines(syncStatus)],
          // Task 10 / fix round 1 I1: the redacted lifecycle surface
          // reaches the settings tab through the same projection.
          lifecycleStateCounts: syncStatus?.lifecycleStateCounts ?? null,
          pendingLifecycleEventCount: syncStatus?.pendingLifecycleEventCount ?? 0,
          failedAttemptCount: syncStatus?.failedAttemptCount ?? 0,
          lifecycleBlockedReasonCodes: syncStatus?.lifecycleBlockedReasonCodes ?? [],
          lastJournalFailureReasons: this.#queueDriver?.readJournalFailureReasons() ?? [],
          generationPublishFailureCount: generationPublishFailures?.count ?? 0,
          lastGenerationPublishFailureReasons: generationPublishFailures?.lastReasons ?? [],
          syncStopReasonTokens: deriveSyncStopReasonTokens(trailEntries),
          trailTailEntries: trailEntries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT),
          trailEntryCount: trailEntries.length,
          trailAppendFailureCount: this.#diagnosticTrail?.readAppendFailureCount() ?? 0,
          // Closed-reason surfacing C1 P3: the closed policy integrity
          // state (including `policy_integrity_failed`) reaches the
          // settings tab, which renders one fixed guidance line per value.
          policyState: this.#policyState,
          // C1 P1: the closed tokens of the last journal startup failure —
          // null before the first failure, never a fake success token.
          lastStartupFailureTokens: this.#lastStartupFailureTokens,
          localNoteSyncStatuses: this.#readLocalNoteSyncStatuses(),
          // Device cursor task 12: the closed device-sync status (or null
          // while no coordinator runs / the read failed closed).
          deviceSyncStatus: this.#readDeviceSyncStatus()
        };
      },
      setServerOrigin: (origin) => {
        this.#settings.server_origin = origin;
        void this.#persistSettings();
      },
      setDeviceName: (name) => {
        this.#settings.device_name = name;
        void this.#persistSettings();
      },
      login: () => controller.login(),
      retryConnection: () => this.#retryConnection(policySession, session),
      openBrowserAgain: () => controller.openBrowserAgain(),
      cancelPendingLogin: () => controller.cancelPendingLogin(),
      disconnect: () => session.disconnect()
    });
    this.addSettingTab(this.#settingTab);
    this.addCommand({
      id: "copy-sync-diagnostics",
      name: "Copy sync diagnostics",
      callback: () => {
        void this.#copySyncDiagnostics().catch(() => {
          this.#recordDiagnosticsCopyFailureTrailEntry();
        });
      }
    });
    this.addCommand({
      id: "run-sync-self-check",
      name: "Run sync self-check",
      callback: () => {
        void this.#runSyncSelfCheck();
      }
    });
    this.addCommand({
      id: "retry-connection",
      name: "Retry connection",
      checkCallback: (checking) => {
        if (!this.#isRetryConnectionAvailable(secretStore)) {
          return false;
        }
        if (!checking) {
          void this.#retryConnection(policySession, session);
        }
        return true;
      }
    });
    this.addCommand({
      id: "open-conflict-inbox",
      name: "Open Conflict Inbox",
      checkCallback: (checking) => {
        const controller2 = this.#conflictController;
        if (controller2 === null) {
          return false;
        }
        if (!checking) {
          new ConflictInboxModal(this.app, controller2).open();
        }
        return true;
      }
    });
    const startupRecord = readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME);
    const startupAction = resolveStartupAction(startupRecord);
    if (startupAction === "resume_pending_grant") {
      void controller.resumePendingGrant().catch((error) => {
        this.#recordStartupChainFailure(error);
      });
    } else {
      try {
        await controller.reconcileCrashWindow();
      } catch (error) {
        this.#recordStartupChainFailure(error);
      }
      if (startupAction === "refresh_credential") {
        void session.refresh().then(
          () => refreshVerifiedPolicyAndRequestSnapshot({
            readAcceptedRevisionNumber: () => policySession.acceptedState?.revisionNumber ?? null,
            refresh: () => policySession.refresh(),
            requestSnapshot: (reason) => this.#requestAutomaticSnapshot(reason)
          })
        ).catch((error) => {
          this.#recordStartupChainFailure(error);
        });
      }
    }
    await this.#startJournalCapture();
  }
  onunload() {
    this.#clearScheduledRetryPassTrigger();
    const deviceSyncCoordinatorStop = this.#syncCoordinator?.stop() ?? Promise.resolve();
    this.#syncCoordinator = null;
    const automaticSnapshotStop = this.#automaticSnapshotCoordinator?.stop() ?? Promise.resolve();
    this.#automaticSnapshotCoordinator = null;
    const boundedQueuePassStop = this.#boundedQueuePassDispatcher?.stop() ?? Promise.resolve();
    this.#boundedQueuePassDispatcher = null;
    this.#queueDriver?.stop();
    this.#lifecycleCapture?.dispose();
    this.#capture?.dispose();
    const captureQuiescence = this.#capture?.whenIdle() ?? Promise.resolve();
    void Promise.all([deviceSyncCoordinatorStop, automaticSnapshotStop, boundedQueuePassStop, captureQuiescence]).then(() => {
      this.#releaseJournalResources();
    });
    this.#controller?.stop();
    this.#session?.clearMemoryAccess();
  }
  #releaseJournalResources() {
    this.#journalPersistence?.attemptFinalFlush();
    this.#journalPersistence?.close();
    this.#journalPersistence = null;
    this.#queueDriver = null;
    this.#lifecycleCapture = null;
    this.#capture = null;
    this.#queueRepository = null;
    this.#diagnosticTrail = null;
    this.#syncCoordinator = null;
    this.#syncStatusBarItem = null;
    this.#conflictController = null;
    this.#conflictDiagnostics = null;
    this.#conflictRepository = null;
  }
  #resolveHasActiveCredential(secretStore) {
    return this.#session !== null && readDeviceSecretRecord(secretStore, DEVICE_CREDENTIAL_RECORD_NAME)?.state === "active";
  }
  /**
   * The retry affordance gate (plugin hygiene, 2026-08-16 §12): enabled
   * exactly while offline WITH an active credential — the one recoverable
   * state that previously required a plugin reload. The `canLogin` gating is
   * unchanged.
   */
  #isRetryConnectionAvailable(secretStore) {
    return this.#connectionState === "offline" && this.#resolveHasActiveCredential(secretStore);
  }
  /**
   * Re-invoke the bounded session refresh chain on explicit demand (plugin
   * hygiene, 2026-08-16 §12): one token refresh, then the verified-policy
   * refresh and snapshot request — the same bounded chain the startup action
   * runs. An exceptional rejection routes into the closed startup-failure
   * trail path (buffered until the trail loads); the refresh's own failure
   * state and closed code already ride the state seam.
   */
  async #retryConnection(policySession, tokenSession) {
    const retryChain = tokenSession.refresh().then(
      () => refreshVerifiedPolicyAndRequestSnapshot({
        readAcceptedRevisionNumber: () => policySession.acceptedState?.revisionNumber ?? null,
        refresh: () => policySession.refresh(),
        requestSnapshot: (reason) => this.#requestAutomaticSnapshot(reason)
      })
    );
    void retryChain.catch((error) => {
      this.#recordStartupChainFailure(error);
    });
  }
  #setConnectionState(state, detail) {
    this.#connectionState = state;
    this.#statusDetail = detail;
    this.#settingTab?.display();
    this.#refreshSyncStatus();
  }
  /**
   * A Vault event is actionable only after the plugin has a verified policy
   * snapshot (or its previously verified offline cache). Obsidian emits
   * create/modify notifications while restoring an existing Vault at plugin
   * load; treating those as fresh captures would silently become a full-Vault
   * scan and fail closed at revision 0 before onboarding can establish trust.
   */
  #canCaptureVaultChanges() {
    return this.#policyState === "policy_ready" || this.#policyState === "policy_offline_cached";
  }
  #requestAutomaticSnapshot(reason) {
    const coordinator = this.#automaticSnapshotCoordinator;
    if (coordinator === null) {
      this.#pendingAutomaticSnapshotReason = reason;
      return;
    }
    coordinator.request(reason);
  }
  /**
   * Forward one closed device-sync trigger to the single coordinator
   * (task 12). Null-safe by construction: triggers from Vault events, the
   * visibility surface and the repair command may arrive before the
   * journal starts or after unload, and a missing coordinator simply
   * drops the trigger (the cadence re-requests the work).
   */
  #requestDeviceSyncCycle(trigger) {
    this.#syncCoordinator?.request(trigger);
  }
  /**
   * The ONE conflict recovery trigger (conflict inbox task 9): retry the
   * persisted local applies of committed conflict resolutions — local
   * application only, never another resolution, never a conflict poll.
   * Null-safe by construction (no controller before the journal starts or
   * after unload), fire-and-forget with a closed-token catch: a rejection
   * of the retry surface itself reaches the trail as
   * `conflict_apply_retry_failed`, and the status refresh keeps the
   * parked-apply surface honest on both branches.
   */
  #retryConflictLocalApplies() {
    const controller = this.#conflictController;
    if (controller === null) {
      return;
    }
    void controller.retryPendingLocalApplies().catch(() => {
      this.#conflictDiagnostics?.observeConflictCompositionFailure("conflict_apply_retry_failed");
    }).finally(() => {
      this.#refreshSyncStatus();
    });
  }
  /**
   * The closed device-sync status of the settings snapshot and the
   * diagnostics export (task 12), or null when no coordinator runs. The
   * read is fail-closed: a throwing projection reports the once-per-
   * session closed `composition_read_failure` observation through the
   * existing sync-status read site and never becomes a stop reason — the
   * settings render keeps its "not running" line instead of a partial or
   * wrong status.
   */
  #readDeviceSyncStatus() {
    const coordinator = this.#syncCoordinator;
    if (coordinator === null) {
      return null;
    }
    try {
      return coordinator.readStatus();
    } catch {
      this.#reportSyncStatusReadFailureOnce();
      return null;
    }
  }
  async #persistSettings() {
    const loaded = await this.loadData();
    await this.saveData({ ...loaded ?? {}, ...this.#settings });
  }
  /**
   * The narrow journal binary store of the journal design (6.1): journal
   * generations resolve through the Vault's configured plugin directory
   * (`Vault.configDir` + the manifest id) and the adapter's binary methods —
   * never a hard-coded config-directory name. Composition only; the journal
   * persistence layer itself is injected and tested in `./journal`.
   */
  createJournalFileStore() {
    return createVaultPluginJournalStore(this.app, this.manifest.id);
  }
  /**
   * Composition-only journal capture and queue wiring (journal design 7.1,
   * 8): load the vendored engine, run journal recovery, then — and only
   * then — register the Vault listeners, the bounded foreground queue
   * driver and the restore command. Every behavior lives in the tested
   * `./journal` modules; this method only binds real adapters.
   */
  async #startJournalCapture() {
    const policySession = this.#policySession;
    const session = this.#session;
    if (policySession === null || session === null) {
      return;
    }
    let startupStage = "other";
    try {
      const diagnosticTrail = createSyncDiagnosticsTrail({
        fileStore: this.createJournalFileStore()
      });
      await diagnosticTrail.load();
      this.#diagnosticTrail = diagnosticTrail;
      const journalFailureReporter = createJournalFailureReporter(diagnosticTrail);
      this.#journalFailureReporter = journalFailureReporter;
      this.#flushBufferedStartupFailureEntries(diagnosticTrail);
      startupStage = "wasm_read";
      const engineWasmBinary = await this.#readJournalEngineWasmBinary();
      startupStage = "engine_load";
      const engineModule = await loadVendoredSqliteEngine({
        wasmBinary: engineWasmBinary
      });
      const persistence = new JournalPersistence({
        fileStore: this.createJournalFileStore(),
        engineModule,
        diagnosticTrail,
        // A journal rebuilt over a non-empty Vault must reconcile first
        // (the mobile full-deletion shape); the probe mirrors exactly the
        // files the automatic snapshot would admit.
        hasVaultContent: async () => this.app.vault.getFiles().length > 0
      });
      startupStage = "journal_recovery";
      await persistence.open();
      startupStage = "other";
      const journalDatabase = {
        runSerializedMutation(operation) {
          return persistence.commitGeneration(operation);
        },
        readAll(sql) {
          return persistence.readAll(sql);
        }
      };
      const createJournalId = createUuidv7Factory();
      const deviceSyncRepository = new DeviceSyncRepository({ database: journalDatabase });
      const repository = new JournalRepository({
        database: journalDatabase,
        createId: createJournalId,
        createDeviceSyncRepository: () => deviceSyncRepository,
        // Spec 12.4: completing a device repair must clear the persistence
        // sticky reconcile flag through this callback, or every later
        // generation commit re-clobbers the durable clear and re-arms the
        // reconcile loop (the 2026-09-01 live-round wedge).
        onDeviceSyncRepairComplete: () => persistence.markReconcileComplete()
      });
      const echoSuppressor = createEchoSuppressor({
        repository: repository.deviceSync,
        database: journalDatabase
      });
      const vaultReader = this.#createCaptureVaultReader();
      const lifecycleVaultReader = this.#createLifecycleVaultReader(vaultReader);
      const lifecycleCapture = new LifecycleCaptureImpl({
        repository,
        lifecycle: repository.lifecycle,
        vaultReader: lifecycleVaultReader,
        createId: createJournalId,
        policyRevision: 1,
        failureReporter: journalFailureReporter,
        echoSuppressor
      });
      await lifecycleCapture.resumePendingRenameIntents();
      const capture = new JournalCapture({
        repository,
        vaultReader,
        policyGate: policySession,
        lifecycleCapture,
        failureReporter: journalFailureReporter,
        echoSuppressor
      });
      const lifecycleDriver = new LifecycleDriverImpl({
        repository,
        lifecycle: repository.lifecycle,
        diagnosticTrail,
        onPendingRenameIntentReady: (localFileId) => {
          lifecycleCapture.rearmPendingRenameIntent(localFileId);
        },
        api: createRequestUrlLifecycleApi({
          // Resolved afresh per commit so a server-origin edit in settings
          // applies without a plugin reload (the sync API's resolveOrigin
          // contract); freezing it at load stranded every lifecycle commit
          // on a fresh install whose origin was entered after loading.
          resolveBaseUrl: () => parseServerOrigin(this.#settings.server_origin, {
            allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
          }) ?? "",
          transport: createRequestUrlTransport((request) => (0, import_obsidian5.requestUrl)(request)),
          resolveAccessToken: () => session.accessCredential
        })
      });
      const queueDriver = new JournalQueueDriver({
        repository,
        syncApi: createJournalSyncApi({
          transport: createObsidianSyncHttpTransport(),
          resolveOrigin: () => parseServerOrigin(this.#settings.server_origin, {
            allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
          }) ?? "",
          getAccessToken: () => session.accessCredential
        }),
        fileBytesReader: vaultReader,
        lifecycleDriver,
        multipartPlatform: resolveMultipartPlatformClass(),
        refreshAccessToken: () => session.refresh(),
        diagnosticTrail
      });
      const boundedQueuePassDispatcher = new CoalescingQueuePassDispatcher({
        runPass: async () => {
          return await this.#runBoundedQueuePass();
        }
      }, journalFailureReporter);
      const automaticSnapshotCoordinator = new AutomaticSnapshotCoordinator({
        runSnapshot: async (signal) => {
          if (signal.aborted || !this.#canCaptureVaultChanges()) {
            return { outcome: "skipped", queuedEventCount: 0 };
          }
          const snapshot = this.#projectSyncStatus();
          if (snapshot === null || snapshot.kind === "reconcile_required") {
            return { outcome: "stopped", queuedEventCount: 0 };
          }
          const summary = await capture.runAutomaticSnapshot({ signal });
          if (signal.aborted) {
            return { outcome: "stopped", queuedEventCount: 0 };
          }
          this.#refreshSyncStatus();
          let queuedEventCount = summary.queuedEventCount;
          try {
            queuedEventCount = Math.max(
              summary.queuedEventCount,
              repository.countPendingEvents()
            );
          } catch {
            this.#recordStatusReadFailureOnce();
          }
          return {
            outcome: summary.outcome === "completed" ? "completed" : "stopped",
            queuedEventCount
          };
        },
        requestQueuePass: async () => {
          await boundedQueuePassDispatcher.request();
        }
      }, journalFailureReporter);
      const deviceSyncDiagnostics = createDeviceSyncDiagnostics(diagnosticTrail);
      const deviceSyncApi = createDeviceSyncApi({
        transport: createObsidianDeviceSyncHttpTransport(),
        resolveOrigin: () => parseServerOrigin(this.#settings.server_origin, {
          allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
        }) ?? "",
        getAccessToken: () => session.accessCredential,
        // Resident-app self-healing: a foreground session whose access
        // credential expired rotates once and retries instead of silently
        // stopping until an app restart (2026-08-27 physical-matrix finding).
        refreshAccessToken: () => session.refresh(),
        diagnostics: deviceSyncDiagnostics
      });
      const remoteEventApplier = createRemoteEventApplier({
        repository: deviceSyncRepository,
        writer: new AtomicVaultWriterImpl({
          repository: deviceSyncRepository,
          seam: createStructuralVaultMutationSeam(
            this.#createStructuralVaultSurfaceForDeviceSync(),
            this.#createStructuralVaultAdapterSurfaceForDeviceSync()
          )
        }),
        downloader: (input) => deviceSyncApi.downloadSourceVersion(input),
        diagnostics: deviceSyncDiagnostics
      });
      const manifestReconcilerJournal = createManifestReconcilerJournal({
        repository,
        capture
      });
      const manifestReconciler = createManifestReconciler({
        repository: deviceSyncRepository,
        api: deviceSyncApi,
        capture: createManifestCapture({
          vaultReader,
          identityReader: repository
        }),
        journal: manifestReconcilerJournal,
        applier: remoteEventApplier,
        diagnostics: deviceSyncDiagnostics,
        downloader: (input) => deviceSyncApi.downloadSourceVersion(input)
      });
      const syncCoordinator = createSyncCoordinator({
        repository: deviceSyncRepository,
        api: deviceSyncApi,
        applier: remoteEventApplier,
        reconciler: manifestReconciler,
        outbound: boundedQueuePassDispatcher,
        diagnostics: deviceSyncDiagnostics,
        nowEpochMs: () => Date.now(),
        // The journal's sticky reconcile flag joins the repair-if-required
        // decision of every cycle.
        isJournalReconcileRequired: () => persistence.isReconcileRequired,
        // The active manifest run's action progress feeds the pending
        // action count of the closed status projection.
        readManifestActionProgress: () => repository.readManifestActionProgress(),
        // The server-minted device id (uuid7, persisted at grant
        // exchange) is the identity the device-event origin_device_id
        // namespace carries; the client_instance_id is a disjoint
        // client-minted namespace that can never match. Null before the
        // first exchange: the self-origin check then never suppresses and
        // every pulled event walks the full crash-safe apply machine.
        resolveOwnDeviceId: () => this.#settings.device_id,
        outboundEvidence: {
          readCommittedOutboundRowByLocator: (normalizedLocator) => repository.readLocalFileByPath(normalizedLocator)
        },
        // After a suspension of one hour or more, an active manifest run's
        // temporary progress is discarded before the resume starts a
        // fresh checkpoint-bound run under the same barrier.
        discardExpiredManifestRun: () => manifestReconcilerJournal.discardActiveManifestRun()
      });
      this.#syncCoordinator = syncCoordinator;
      this.#journalPersistence = persistence;
      this.#capture = capture;
      this.#queueDriver = queueDriver;
      this.#queueRepository = repository;
      this.#lifecycleCapture = lifecycleCapture;
      this.#automaticSnapshotCoordinator = automaticSnapshotCoordinator;
      this.#boundedQueuePassDispatcher = boundedQueuePassDispatcher;
      const conflictDiagnostics = createConflictDiagnosticsTrailSink(diagnosticTrail);
      const conflictRepository = new ConflictRepository({ database: journalDatabase });
      const conflictApi = createConflictApi({
        transport: createObsidianDeviceSyncHttpTransport(),
        resolveOrigin: () => parseServerOrigin(this.#settings.server_origin, {
          allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
        }) ?? "",
        getAccessToken: () => session.accessCredential
      });
      const conflictController = observeUnobservedConflictControllerFailures(
        createConflictController({
          api: conflictApi,
          repairStore: conflictRepository,
          // The real verified-candidate upload surface (Task 10): a
          // save_merged draft first becomes a verified object through the
          // open conflict's candidate route and the resolve carries only
          // the opaque reference.
          uploader: createConflictVerifiedCandidateUploader(conflictApi),
          applier: createConflictCanonicalOutcomeApplier({
            database: journalDatabase,
            repository: deviceSyncRepository,
            seam: createStructuralVaultMutationSeam(
              this.#createStructuralVaultSurfaceForDeviceSync(),
              this.#createStructuralVaultAdapterSurfaceForDeviceSync()
            ),
            downloadSourceVersion: (input) => deviceSyncApi.downloadSourceVersion(input),
            diagnostics: conflictDiagnostics
          }),
          diagnostics: conflictDiagnostics
        }),
        conflictDiagnostics
      );
      this.#conflictController = conflictController;
      this.#conflictDiagnostics = conflictDiagnostics;
      this.#conflictRepository = conflictRepository;
      this.app.workspace.onLayoutReady(() => {
        this.registerEvent(
          this.app.vault.on("create", (file) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            void capture.notifyPathChanged(file.path).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => void 0
            );
          })
        );
        this.registerEvent(
          this.app.vault.on("modify", (file) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            void capture.notifyPathChanged(file.path).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => void 0
            );
          })
        );
        this.registerEvent(
          this.app.vault.on("delete", (file) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            void capture.notifyPathDeleted(this.#toVaultTargetFile(file)).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => void 0
            );
          })
        );
        this.registerEvent(
          this.app.vault.on("rename", (file, oldPath) => {
            if (!this.#canCaptureVaultChanges()) {
              return;
            }
            void capture.notifyPathRenamed(this.#toVaultRenameTarget(file), oldPath).then(
              () => {
                void boundedQueuePassDispatcher.request();
                this.#requestDeviceSyncCycle("local_commit");
              },
              () => void 0
            );
          })
        );
        automaticSnapshotCoordinator.request("startup");
        if (this.#pendingAutomaticSnapshotReason !== null) {
          automaticSnapshotCoordinator.request(this.#pendingAutomaticSnapshotReason);
          this.#pendingAutomaticSnapshotReason = null;
        }
        this.#requestDeviceSyncCycle("startup");
        void this.#retryConflictLocalApplies();
        this.registerDomEvent(document, "visibilitychange", () => {
          if (document.visibilityState === "visible") {
            this.#requestDeviceSyncCycle("resume");
            void this.#retryConflictLocalApplies();
          }
        });
      });
      this.addCommand({
        id: "restore-selected-tombstone",
        name: "Restore selected tombstone",
        callback: () => {
          void this.#runRestoreSelectedTombstone();
        }
      });
      this.addCommand({
        id: "repair-sync",
        name: "Repair sync",
        callback: () => {
          this.#requestDeviceSyncCycle("explicit_repair");
        }
      });
    } catch (error) {
      const startupFailureTokens = this.#buildStartupFailureTokens(startupStage, error);
      this.#lastStartupFailureTokens = startupFailureTokens;
      this.#appendStartupFailureTrailEntry(startupFailureTokens);
    }
  }
  // --- closed startup-failure and read-swallow surfacing (C1 P1/P4/P5) ---------------------------
  /**
   * Build the closed token list of one startup failure (C1 P1): the failed
   * stage token plus the closed `JournalStoreErrorReason` when the thrown
   * value is a store error. Closed tokens only — the exception text, any
   * path and any raw detail never enter the list.
   */
  #buildStartupFailureTokens(startupStage, error) {
    const tokens = [startupStage];
    if (error instanceof JournalStoreError) {
      tokens.push(error.reason);
    }
    return tokens;
  }
  /**
   * Append one `startup_failure` trail entry (fire-and-forget, the trail's
   * never-blocks guarantee holds) or buffer it when the trail does not
   * exist yet (C1 P4: the startup chains can reject before the sidecar is
   * loaded). The buffer is bounded; the oldest entries drop beyond it.
   */
  #appendStartupFailureTrailEntry(tokens) {
    const trail = this.#diagnosticTrail;
    if (trail === null) {
      if (this.#bufferedStartupFailureTokenLists.length < MAX_BUFFERED_STARTUP_FAILURE_ENTRIES) {
        this.#bufferedStartupFailureTokenLists.push(tokens);
      }
      return;
    }
    void trail.append({ kind: "startup_failure", tokens });
  }
  /**
   * Flush the bounded pre-trail startup-failure buffer into the freshly
   * loaded trail (C1 P4). Each buffered list appends exactly once; entries
   * recorded after this point append directly.
   */
  #flushBufferedStartupFailureEntries(trail) {
    for (const tokens of this.#bufferedStartupFailureTokenLists) {
      void trail.append({ kind: "startup_failure", tokens });
    }
    this.#bufferedStartupFailureTokenLists = [];
  }
  /**
   * Route one exceptional throw of the two fire-and-forget startup chains
   * into the same `startup_failure` trail path (C1 P4): stage token
   * `other` plus the closed store reason when applicable, buffered until
   * the trail exists. The settings snapshot's journal-startup verdict
   * stays untouched — these chains do not stop the journal.
   */
  #recordStartupChainFailure(error) {
    this.#appendStartupFailureTrailEntry(this.#buildStartupFailureTokens("other", error));
  }
  /**
   * Record the pending-count read swallow (C1 P5): ONE
   * `composition_read_failure` trail entry carrying the closed
   * `status_read` stage and the `status_read_failed` token, at most once
   * per session — no per-render spam, and never a derived stop reason
   * (trail v2 taxonomy, task 7).
   */
  #recordStatusReadFailureOnce() {
    if (this.#hasRecordedStatusReadFailure) {
      return;
    }
    this.#hasRecordedStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["status_read", "status_read_failed"]
      });
    }
  }
  /**
   * Record the note-status read swallow (C1 P5): ONE
   * `composition_read_failure` trail entry carrying the closed
   * `note_status_read` stage and the `note_status_read_failed` token, at
   * most once per session — no per-render spam, and never a derived stop
   * reason (trail v2 taxonomy, task 7).
   */
  #recordNoteStatusReadFailureOnce() {
    if (this.#hasRecordedNoteStatusReadFailure) {
      return;
    }
    this.#hasRecordedNoteStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["note_status_read", "note_status_read_failed"]
      });
    }
  }
  #reportRetryScheduleReadFailureOnce() {
    if (this.#hasReportedRetryScheduleReadFailure) {
      return;
    }
    this.#hasReportedRetryScheduleReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["retry_schedule_read", "retry_schedule_read_failed"]
      });
    }
  }
  #reportSyncStatusReadFailureOnce() {
    if (this.#hasReportedSyncStatusReadFailure) {
      return;
    }
    this.#hasReportedSyncStatusReadFailure = true;
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({
        kind: "composition_read_failure",
        tokens: ["sync_status_read", "sync_status_read_failed"]
      });
    }
  }
  /**
   * The explicit-restore command callback (Task 10, spec 6.3 + 7.1):
   * show the picker for retained tombstones, confirm the target path
   * with the user and call the lifecycle capture port to validate the
   * bytes hash and record the restore event. Failures surface as the
   * closed `journal_mutation_failed` `JournalStoreErrorReason` and
   * never reach the console; the sync status is refreshed on both
   * branches so the redacted status surface reflects the new lifecycle
   * state.
   */
  async #runRestoreSelectedTombstone() {
    const lifecycleCapture = this.#lifecycleCapture;
    const repository = this.#queueRepository;
    if (lifecycleCapture === null || repository === null) {
      return;
    }
    const selection = await this.#pickTombstonedFile(repository);
    if (selection === null) {
      return;
    }
    const targetPath = await this.#promptForRestoreTargetPath();
    if (targetPath === null) {
      return;
    }
    let reservation;
    try {
      reservation = await lifecycleCapture.reserveRestoreTarget(
        selection.localFileId,
        targetPath
      );
    } catch {
      this.#journalFailureReporter?.reportJournalFailure("restore_reservation_persist_failed");
      new import_obsidian5.Notice("Restore could not be recorded. Check the Sync status.");
      this.#refreshSyncStatus();
      return;
    }
    if (reservation.outcome === "refused") {
      this.#journalFailureReporter?.reportJournalFailure(reservation.reason);
      new import_obsidian5.Notice(RESTORE_RESERVATION_REFUSAL_NOTICES[reservation.reason]);
      this.#refreshSyncStatus();
      return;
    }
    const confirmation = await this.#confirmRestoreRequest(selection, targetPath);
    if (confirmation !== "confirmed") {
      if (confirmation === "cancelled") {
        await lifecycleCapture.releaseRestoreTarget(selection.localFileId).catch(
          () => void 0
        );
        this.#refreshSyncStatus();
        return;
      }
      new import_obsidian5.Notice("Restore target reserved. Re-run the command to resume or cancel.");
      this.#refreshSyncStatus();
      return;
    }
    try {
      await lifecycleCapture.requestRestore(selection.localFileId, targetPath);
    } catch {
    }
    void this.#boundedQueuePassDispatcher?.request();
    this.#refreshSyncStatus();
  }
  /**
   * The narrow picker for retained tombstones. Each candidate carries
   * only its plugin-local `localFileId` and a short safe label — the
   * underlying path never reaches the picker text. The picker closes
   * with `null` when the user dismisses the modal without a choice.
   */
  #pickTombstonedFile(repository) {
    return new Promise((resolve) => {
      const localFileIds = repository.readRestorableLocalFileIds();
      const candidates = localFileIds.map((localFileId) => ({
        localFileId,
        shortLabel: `Tombstone #${localFileId.slice(-8)}`
      }));
      if (candidates.length === 0) {
        new NoticeModal(
          this.app,
          "No retained tombstones",
          "There are no tombstoned files eligible for restore right now."
        ).open();
        resolve(null);
        return;
      }
      const modal = new SuggestModal(
        this.app,
        candidates,
        (item) => item.shortLabel
      );
      modal.setPlaceholder("Pick a tombstone to restore");
      modal.onChooseItem = (item) => resolve(item);
      modal.onClose = () => resolve(null);
      modal.open();
    });
  }
  /** The narrow text prompt for the restore target path. */
  #promptForRestoreTargetPath() {
    return new Promise((resolve) => {
      const modal = new TextPromptModal(
        this.app,
        "Restore target path",
        "Vault path the restored bytes should occupy. The path is reserved when you continue; place the restored bytes there before confirming.",
        (value) => resolve(value),
        () => resolve(null)
      );
      modal.open();
    });
  }
  /** The narrow confirmation modal of an explicit restore request. */
  #confirmRestoreRequest(selection, targetPath) {
    return new Promise((resolve) => {
      const modal = new ConfirmModal(
        this.app,
        "Confirm restore",
        [
          `Restore ${selection.shortLabel} to the chosen Vault path?`,
          "The bytes hash must match the server-committed content hash or the restore is rejected."
        ].join("\n"),
        () => resolve("confirmed"),
        () => resolve("cancelled"),
        () => resolve("dismissed")
      );
      void targetPath;
      modal.open();
    });
  }
  /**
   * The copy-sync-diagnostics command callback (sync error tracing task 2):
   * build the sanitized export block — closed tokens, counts and ISO
   * timestamps only — and place it on the clipboard. When the clipboard is
   * unavailable or rejects the write, the SAME block is shown in a
   * read-only preformatted modal. The block never reaches a console, a
   * log or any other surface.
   */
  async #copySyncDiagnostics() {
    const block = this.#buildSyncDiagnosticsExportBlock();
    const clipboard = navigator.clipboard;
    if (clipboard !== void 0) {
      try {
        await clipboard.writeText(block);
        new import_obsidian5.Notice("Sync diagnostics copied to the clipboard.");
        return;
      } catch {
      }
    }
    new PreformattedTextModal(this.app, "Sync diagnostics", block).open();
  }
  /**
   * Record the copy command's own rejection (child six deferred
   * remediation): the clipboard-unavailable/refused branch is already
   * absorbed by the modal fallback inside `#copySyncDiagnostics`, so this
   * handler covers an exceptional rejection of the copy pipeline itself.
   * It reports through the established bounded diagnostics pattern — ONE
   * `self_check` trail entry carrying the closed `trail_persist_failed`
   * verdict token (the diagnostics write-out pipeline failed; the
   * append itself is failure-counted and never rejects). The handler
   * never rethrows into UI processing and never logs the clipboard data
   * or any other value: the closed token carries no detail.
   */
  #recordDiagnosticsCopyFailureTrailEntry() {
    const trail = this.#diagnosticTrail;
    if (trail !== null) {
      void trail.append({ kind: "self_check", tokens: ["trail_persist_failed"] });
    }
  }
  /**
   * The closed input assembly of the sanitized export block: the current
   * status snapshot line, the settings journal-store diagnostics inputs,
   * the aggregate trail counts and the trail tail. Every source is an
   * already-redacted closed surface; the builder adds no raw value.
   */
  #buildSyncDiagnosticsExportBlock() {
    const syncStatus = this.#projectSyncStatus();
    const generationPublishFailures = this.#journalPersistence?.readGenerationPublishFailureSummary() ?? null;
    const trailEntries = this.#diagnosticTrail?.readEntries() ?? [];
    const deviceSyncStatus = this.#readDeviceSyncStatus();
    return renderSyncDiagnosticsExportBlock({
      syncStatusLine: syncStatus === null ? null : renderJournalSyncStatus(syncStatus),
      syncBlockerGuidance: syncStatus === null ? [] : [...syncBlockerGuidanceLines(syncStatus)],
      journalStoreDiagnostics: {
        lastJournalFailureReasons: this.#queueDriver?.readJournalFailureReasons() ?? [],
        generationPublishFailureCount: generationPublishFailures?.count ?? 0,
        lastGenerationPublishFailureReasons: generationPublishFailures?.lastReasons ?? []
      },
      trailEntryCount: trailEntries.length,
      trailAppendFailureCount: this.#diagnosticTrail?.readAppendFailureCount() ?? 0,
      trailTail: trailEntries.slice(-SYNC_DIAGNOSTICS_TRAIL_TAIL_ENTRY_LIMIT),
      deviceSyncStatusLine: deviceSyncStatus === null ? null : renderDeviceSyncStatusText(deviceSyncStatus)
    });
  }
  /**
   * The run-sync-self-check command callback (sync error tracing task 3):
   * execute the three closed-verdict steps — trail append-and-persist
   * probe, credential presence, origin reachability — and show the
   * one-line summary in a notice. The composition holds no sync-mutating
   * capability: the pure runner receives only the trail port, the boolean
   * credential-presence reader and one liveness GET through the existing
   * requestUrl transport seam toward the SAME resolved origin the sync
   * client uses. Any outcome — including an unreachable or hanging origin —
   * closes as a verdict token; no hostname, status number or response text
   * ever reaches the notice.
   */
  async #runSyncSelfCheck() {
    const trail = this.#diagnosticTrail;
    const startupFailureTokens = this.#lastStartupFailureTokens;
    if (trail === null || startupFailureTokens !== null) {
      new import_obsidian5.Notice(renderSyncSelfCheckJournalNotRunningText(startupFailureTokens), 1e4);
      return;
    }
    const transport = createObsidianPolicyHttpTransport();
    const summary = await runSyncSelfCheck({
      trail,
      hasAccessCredential: () => this.#session?.accessCredential != null,
      probeOrigin: async () => {
        const origin = parseServerOrigin(this.#settings.server_origin, {
          allowLoopbackHttp: ALLOW_LOOPBACK_HTTP_ORIGIN
        }) ?? "";
        if (origin === "") {
          throw new Error("origin unconfigured");
        }
        await transport({ url: `${origin}/api/health/live`, headers: {} });
      }
    });
    new import_obsidian5.Notice(renderSyncSelfCheckSummaryText(summary), 1e4);
  }
  /**
   * The single bounded queue-pass wrapper (spec 8, 11): settled Vault events
   * and automatic snapshots funnel through here, so the status projection
   * sees the active pass and every finished pass outcome. Only the automatic
   * snapshot dispatcher awaits it; listeners remain fire-and-forget.
   */
  async #runBoundedQueuePass() {
    const driver = this.#queueDriver;
    if (driver === null) {
      return { outcome: "completed", processedEventCount: 0 };
    }
    this.#isQueuePassActive = true;
    this.#refreshSyncStatus();
    let summary;
    try {
      summary = await driver.requestPass();
    } catch {
      summary = { outcome: "pass_wrapper_failed", processedEventCount: 0 };
      const trail = this.#diagnosticTrail;
      if (trail !== null) {
        void trail.append({ kind: "pass_outcome", tokens: ["pass_wrapper_failed"] });
      }
    }
    if (summary.outcome !== "pass_already_running") {
      this.#isQueuePassActive = false;
      this.#lastQueuePassOutcome = summary.outcome;
      if (summary.outcome !== "stopped") {
        this.#armScheduledRetryPassTrigger();
      }
    }
    this.#refreshSyncStatus();
    return summary;
  }
  /**
   * The bounded one-shot scheduled retry trigger (fix round 2 D4, widened
   * in fix round 3, stopped-exclusion added in fix round 4): after any
   * pass that actually ran and did not end `stopped`, arm ONE cancellable
   * timer at the earliest pending retry deadline (plus a small safety
   * margin) whose single firing requests one bounded queue pass through
   * the same dispatcher every other trigger uses. This mirrors the
   * already-reviewed `deadline_reached` serial follow-up: the PASS stays
   * bounded and trigger-driven; only the trigger is scheduled. The armer
   * no-ops when no pending row carries a retry deadline. A `stopped` pass
   * end never arms: the dispatcher is not stopped (only unload stops it),
   * so a stopped-pass timer would fire into the stopped driver and
   * self-sustain at a past deadline. At most one timer is outstanding
   * (an already-earlier target keeps the existing timer, a sooner target
   * re-arms it), unload cancels it, and this is never a repeating daemon
   * loop. No `JournalQueueDriver` failure semantics change — the
   * no-overtake discipline of fix round 1 stays.
   */
  #armScheduledRetryPassTrigger() {
    const repository = this.#queueRepository;
    if (repository === null) {
      return;
    }
    let earliestRetryEpochMs = null;
    try {
      earliestRetryEpochMs = repository.readEarliestPendingRetryEpochMs();
    } catch {
      this.#reportRetryScheduleReadFailureOnce();
      return;
    }
    if (earliestRetryEpochMs === null) {
      return;
    }
    const targetEpochMs = earliestRetryEpochMs + SCHEDULED_RETRY_PASS_SAFETY_MARGIN_MS;
    if (this.#scheduledRetryPassTargetEpochMs !== null && this.#scheduledRetryPassTargetEpochMs <= targetEpochMs) {
      return;
    }
    this.#clearScheduledRetryPassTrigger();
    this.#scheduledRetryPassTargetEpochMs = targetEpochMs;
    this.#scheduledRetryPassTimer = setTimeout(() => {
      this.#scheduledRetryPassTimer = null;
      this.#scheduledRetryPassTargetEpochMs = null;
      void this.#boundedQueuePassDispatcher?.request();
    }, Math.max(0, targetEpochMs - Date.now()));
  }
  /** Cancel the outstanding scheduled retry timer (unload / re-arm). */
  #clearScheduledRetryPassTrigger() {
    if (this.#scheduledRetryPassTimer !== null) {
      clearTimeout(this.#scheduledRetryPassTimer);
      this.#scheduledRetryPassTimer = null;
    }
    this.#scheduledRetryPassTargetEpochMs = null;
  }
  /**
   * Project and render the closed sync status (spec 11): the journal
   * histogram, credential existence and pass facts in; one of the six
   * closed values with counts out — the small status-bar surface and the
   * settings snapshot. A reconcile-required journal is a hard stop: the
   * driver is stopped here and the child-6 guidance explains why nothing
   * syncs until repair.
   */
  #refreshSyncStatus() {
    const snapshot = this.#projectSyncStatus();
    if (snapshot === null) {
      return;
    }
    if (snapshot.kind === "reconcile_required") {
      this.#queueDriver?.stop();
    }
    const statusBarItem = this.#syncStatusBarItem ?? this.addStatusBarItem();
    this.#syncStatusBarItem = statusBarItem;
    statusBarItem.setText(renderJournalSyncStatus(snapshot));
  }
  /**
   * The closed projection input, or null while no journal runs: the
   * composition reads the redacted repository histogram plus the sticky
   * journal reconcile flag, the live credential fact, the pass facts,
   * (Task 10) the redacted source-lifecycle surface (state histogram,
   * pending-event count, failed-attempt count, closed blocker codes) and
   * (multipart task 11) the redacted multipart surface (closed
   * session-state histogram and closed safe-reason tokens of the durable
   * multipart progress). All reads share one `try { … } catch { return
   * null }` boundary so an unreadable journal renders no status rather
   * than a partial one.
   */
  #projectSyncStatus() {
    const repository = this.#queueRepository;
    if (repository === null) {
      return null;
    }
    let eventStateErrorCounts;
    let lifecycleStateCounts;
    let pendingLifecycleEventCount;
    let failedAttemptCount;
    let lifecycleBlockedReasonCodes;
    let multipartSessionStateCounts;
    let multipartSafeReasonCodes;
    let conflictApplyStatusFacts;
    try {
      eventStateErrorCounts = repository.readEventStateErrorCounts();
      lifecycleStateCounts = repository.readLifecycleStateCounts();
      pendingLifecycleEventCount = repository.countPendingLifecycleEvents();
      failedAttemptCount = repository.countFailedAttempts();
      lifecycleBlockedReasonCodes = repository.readLifecycleBlockedReasonCodes();
      multipartSessionStateCounts = repository.readMultipartSessionStateCounts();
      multipartSafeReasonCodes = repository.readMultipartSafeReasonCodes();
      conflictApplyStatusFacts = deriveConflictApplyStatusFacts(
        this.#conflictRepository?.readPendingLocalApplies() ?? []
      );
    } catch {
      this.#reportSyncStatusReadFailureOnce();
      return null;
    }
    return projectJournalSyncStatus({
      isReconcileRequired: this.#journalPersistence?.isReconcileRequired ?? false,
      eventStateErrorCounts,
      lifecycleStateCounts,
      pendingLifecycleEventCount,
      failedAttemptCount,
      lifecycleBlockedReasonCodes,
      multipartSessionStateCounts,
      multipartSafeReasonCodes,
      conflictApplyPendingCount: conflictApplyStatusFacts.pendingLocalApplyCount,
      conflictApplySafeReasonTokens: conflictApplyStatusFacts.localApplySafeReasonTokens,
      hasAccessCredential: this.#session?.accessCredential != null,
      isQueuePassActive: this.#isQueuePassActive,
      lastQueuePassOutcome: this.#lastQueuePassOutcome
    });
  }
  /**
   * Read note paths exclusively for the local settings tab. This deliberately
   * remains outside the redacted aggregate/status-bar projection.
   */
  #readLocalNoteSyncStatuses() {
    try {
      return this.#queueRepository?.readLocalNoteSyncStatuses() ?? [];
    } catch {
      this.#recordNoteStatusReadFailureOnce();
      return [];
    }
  }
  /**
   * The narrow read-only Vault slice capture needs (journal design 7.1):
   * regular files only, resolved through the structural Obsidian surface.
   */
  #createCaptureVaultReader() {
    const vault = this.app.vault;
    return {
      readRegularFileBytes: async (normalizedPath) => {
        const file = vault.getAbstractFileByPath(normalizedPath);
        if (!(file instanceof import_obsidian5.TFile)) {
          return null;
        }
        return new Uint8Array(await vault.readBinary(file));
      },
      listRegularFilePaths: async () => vault.getFiles().map((file) => file.path).sort()
    };
  }
  /**
   * The narrow read-only Vault slice the lifecycle capture needs
   * (journal design 6.3, 7.1): just the current bytes of one regular
   * file for tombstone verification on restore, layered on top of the
   * capture reader so plugin composition stays in one place.
   */
  #createLifecycleVaultReader(captureReader) {
    return {
      readRegularFileBytes: (normalizedPath) => captureReader.readRegularFileBytes(normalizedPath)
    };
  }
  /** Narrow an Obsidian file into the lifecycle capture's rename target. */
  #toVaultRenameTarget(file) {
    return this.#toVaultTargetFile(file);
  }
  /**
   * The narrow structural `Vault` slice the Task 10 atomic writer's seam
   * binds to (device cursor task 12): `app.vault` narrowed member by
   * member, because the Obsidian `Vault.createBinary` returns the created
   * `TFile` where the mobile-loadable structural surface expects `void`.
   * The rename/trash narrowing is safe by construction: the seam only
   * ever passes files it obtained from `getAbstractFileByPath` itself.
   */
  #createStructuralVaultSurfaceForDeviceSync() {
    const vault = this.app.vault;
    return {
      getAbstractFileByPath: (path) => vault.getAbstractFileByPath(path),
      createBinary: async (path, data) => {
        await vault.createBinary(path, data);
      },
      readBinary: async (path) => {
        const file = vault.getAbstractFileByPath(path);
        if (!(file instanceof import_obsidian5.TFile)) {
          throw new Error("device sync read target is not a regular file");
        }
        return vault.readBinary(file);
      },
      rename: (file, newPath) => vault.rename(file, newPath),
      trash: (file, system) => vault.trash(file, system)
    };
  }
  /**
   * The raw data-adapter slice for the writer's hidden siblings: the live
   * Desktop gate proved the Vault index never lists dot-prefixed paths, so
   * their staging/verify/rename/cleanup must ride the adapter. Structurally
   * typed — no `obsidian` import needed beyond the vault instance itself.
   */
  #createStructuralVaultAdapterSurfaceForDeviceSync() {
    const adapter = this.app.vault.adapter;
    return {
      exists: (path) => adapter.exists(path),
      readBinary: (path) => adapter.readBinary(path),
      writeBinary: (path, data) => adapter.writeBinary(path, data),
      rename: (fromPath, toPath) => adapter.rename(fromPath, toPath),
      remove: (path) => adapter.remove(path)
    };
  }
  /** Narrow an Obsidian file into the lifecycle capture's delete target. */
  #toVaultTargetFile(file) {
    const parentPath2 = file.parent?.path ?? null;
    return {
      path: file.path,
      parent: parentPath2 === null ? null : { path: parentPath2 }
    };
  }
  /** Read the vendored engine bytes from the configured plugin directory. */
  async #readJournalEngineWasmBinary() {
    const { configDir, adapter } = this.app.vault;
    const pluginDirectory = [configDir, "plugins", this.manifest.id].filter((segment) => segment.length > 0).join("/");
    return adapter.readBinary(`${pluginDirectory}/${JOURNAL_ENGINE_WASM_FILE_NAME}`);
  }
  /**
   * The narrow settings adapter of spec 18: the accepted policy state lives
   * in ONE versioned plugin-data record under a reserved member. No Vault
   * content, credential or diagnostic path ever enters this record.
   */
  #createPolicyCacheAdapter() {
    return {
      readPolicyCacheRecord: async () => {
        const loaded = await this.loadData();
        return loaded === null ? null : loaded[POLICY_CACHE_PLUGIN_DATA_KEY] ?? null;
      },
      writePolicyCacheRecord: async (record) => {
        const loaded = await this.loadData();
        await this.saveData({
          ...loaded ?? {},
          [POLICY_CACHE_PLUGIN_DATA_KEY]: record
        });
      }
    };
  }
};
var NoticeModal = class extends import_obsidian5.Modal {
  #title;
  #body;
  constructor(app, title, body) {
    super(app);
    this.#title = title;
    this.#body = body;
  }
  onOpen() {
    const { contentEl } = this;
    contentEl.empty();
    this.titleEl.setText(this.#title);
    contentEl.createEl("p", { text: this.#body });
    new import_obsidian5.Setting(contentEl).addButton(
      (button) => button.setButtonText("Close").setCta().onClick(() => this.close())
    );
  }
};
/*! Bundled license information:

@noble/ed25519/index.js:
  (*! noble-ed25519 - MIT License (c) 2019 Paul Miller (paulmillr.com) *)
*/
