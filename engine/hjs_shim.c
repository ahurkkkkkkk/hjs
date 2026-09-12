/*
 * hjs_shim.c - minimal C shim between Mojo and QuickJS for the hjs
 * headless browser. Owns one JSRuntime + JSContext per evaluation and
 * drives the microtask/event loop until the page signals completion.
 *
 * Exported API (called from Mojo via OwnedDLHandle):
 *   hjs_new()                        -> void*  (new engine)
 *   hjs_free(eng)                    -> void
 *   hjs_eval(eng, code, len)         -> int    (0 ok, 1 JS exception)
 *   hjs_eval_get(eng, code, len)     -> int    (0 ok, 1 exception); result
 *                                    JSON string retrievable via hjs_result
 *   hjs_result(eng, out_len*)        -> const char*  (last eval_get result,
 *                                      empty string if none; owned by engine)
 *   hjs_pending(eng)                 -> int    (1 while page not "done")
 *   hjs_pump(eng, ms)                -> int    (run pending jobs/timers up to
 *                                      ms; returns 1 if more work remains)
 *   hjs_mark_done(eng)               -> void   (host calls when page "load"
 *                                      event fired; JS may re-arm by
 *                                      scheduling timers or fetches)
 *   hjs_has_work(eng)                -> int    (1 if timers/ops outstanding)
 *
 * The page script communicates with the host through two host functions
 * injected at startup:
 *   __hjs_http(method, url, body_or_null, headers_json_or_null) -> promise
 *       Fulfilment value: JSON string {status, body}. The host resolves it.
 *   __hjs_log(str)  -> console.log sink (host reads via eval if wanted)
 *
 * DOM: none. A tiny shim in JS (dom_shim.js, bundled by the host) provides
 * document.querySelector/getElementById returning stub elements with
 * textContent, innerText, getAttribute, and a registerElement hook so that
 * scripts that build pages via innerHTML do not crash. This is enough for
 * "read the text" scraping, not for pixel-perfect rendering.
 */
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "quickjs.h"

/* ------------------------------------------------------------------ */
/* Engine state                                                        */
/* ------------------------------------------------------------------ */

typedef struct Timer {
    int64_t when_ms;
    int interval_ms;   /* 0 = one-shot */
    JSValueConst func;
    JSValue func_owned;
    struct Timer *next;
} Timer;

typedef struct HttpOp {
    int id;
    char *method;   /* malloc'd, may be NULL */
    char *url;      /* malloc'd, may be NULL */
    char *body;     /* malloc'd, may be NULL */
    JSValue resolve_owned;
    JSValue reject_owned;
    struct HttpOp *next;
} HttpOp;

typedef struct Engine {
    JSRuntime *rt;
    JSContext *ctx;
    Timer *timers;
    HttpOp *ops;
    int next_op_id;
    int done;          /* host marked page complete */
    int64_t clock_ms;  /* virtual clock advanced by pump */
    char *result;      /* last eval_get result (JSON), malloc'd */
    size_t result_len;
} Engine;

static int64_t now_ms(Engine *e) { return e->clock_ms; }

/* ------------------------------------------------------------------ */
/* Timers                                                              */
/* ------------------------------------------------------------------ */

static void add_timer(Engine *e, int64_t delay, int interval, JSValue func) {
    Timer *t = malloc(sizeof(Timer));
    t->when_ms = now_ms(e) + delay;
    t->interval_ms = interval;
    t->func_owned = func;
    t->func = t->func_owned;
    t->next = e->timers;
    e->timers = t;
}

static Timer *pop_due_timer(Engine *e) {
    Timer **pp = &e->timers;
    int64_t best = -1;
    Timer *best_t = NULL;
    Timer **best_pp = NULL;
    while (*pp) {
        Timer *t = *pp;
        if (t->when_ms <= now_ms(e) && (best < 0 || t->when_ms < best)) {
            best = t->when_ms;
            best_t = t;
            best_pp = pp;
        }
        pp = &t->next;
    }
    if (best_t) {
        *best_pp = best_t->next;
    }
    return best_t;
}

static void free_timer(Engine *e, Timer *t) {
    JS_FreeValue(e->ctx, t->func_owned);
    free(t);
}

static JSValue js_set_timeout(JSContext *ctx, JSValueConst this_val,
                              int argc, JSValueConst *argv) {
    Engine *e = (Engine *)JS_GetContextOpaque(ctx);
    int64_t delay = 0;
    if (argc > 1) {
        double d;
        if (JS_ToFloat64(ctx, &d, argv[1]) == 0) delay = (int64_t)d;
    }
    JSValue func = JS_DupValue(ctx, argv[0]);
    add_timer(e, delay, 0, func);
    return JS_UNDEFINED;
}

static JSValue js_set_interval(JSContext *ctx, JSValueConst this_val,
                               int argc, JSValueConst *argv) {
    Engine *e = (Engine *)JS_GetContextOpaque(ctx);
    int64_t delay = 1;
    if (argc > 1) {
        double d;
        if (JS_ToFloat64(ctx, &d, argv[1]) == 0 && d > 0) delay = (int64_t)d;
    }
    JSValue func = JS_DupValue(ctx, argv[0]);
    add_timer(e, delay, (int)delay, func);
    return JS_UNDEFINED;
}

static JSValue js_clear_timer(JSContext *ctx, JSValueConst this_val,
                              int argc, JSValueConst *argv) {
    /* No-op: timers are identity-less in this shim; pages that clear
     * timers simply stop being scheduled again for one-shots. */
    return JS_UNDEFINED;
}

/* ------------------------------------------------------------------ */
/* HTTP: host performs fetch, JS gets a promise                        */
/* ------------------------------------------------------------------ */

static JSValue js_http(JSContext *ctx, JSValueConst this_val,
                       int argc, JSValueConst *argv) {
    Engine *e = (Engine *)JS_GetContextOpaque(ctx);
    JSValue promise;
    JSValue resolving_funcs[2]; /* QuickJS fills BOTH: [0]=resolve [1]=reject */
    promise = JS_NewPromiseCapability(ctx, resolving_funcs);
    if (JS_IsException(promise)) return JS_EXCEPTION;

    HttpOp *op = calloc(1, sizeof(HttpOp));
    op->id = e->next_op_id++;
    op->resolve_owned = JS_DupValue(ctx, resolving_funcs[0]);
    op->reject_owned = JS_DupValue(ctx, resolving_funcs[1]);

    /* Snapshot method/url/body/headers as C strings for the host. */
    const char *s;
    if (argc > 0 && (s = JS_ToCString(ctx, argv[0]))) { op->method = strdup(s); JS_FreeCString(ctx, s); }
    if (argc > 1 && (s = JS_ToCString(ctx, argv[1]))) { op->url = strdup(s); JS_FreeCString(ctx, s); }
    if (argc > 2 && (s = JS_ToCString(ctx, argv[2]))) { op->body = strdup(s); JS_FreeCString(ctx, s); }
    op->next = e->ops;
    e->ops = op;

    JS_SetPropertyStr(ctx, promise, "__op_id", JS_NewInt32(ctx, op->id));
    JS_FreeValue(ctx, resolving_funcs[0]);
    JS_FreeValue(ctx, resolving_funcs[1]);

    e->done = 0; /* new work: page not complete */
    return promise;
}

/* ------------------------------------------------------------------ */
/* Console                                                             */
/* ------------------------------------------------------------------ */

static JSValue js_log(JSContext *ctx, JSValueConst this_val,
                      int argc, JSValueConst *argv) {
    for (int i = 0; i < argc; i++) {
        const char *s = JS_ToCString(ctx, argv[i]);
        if (s) {
            fprintf(stderr, "[js] %s\n", s);
            JS_FreeCString(ctx, s);
        }
    }
    return JS_UNDEFINED;
}

/* ------------------------------------------------------------------ */
/* Event loop                                                          */
/* ------------------------------------------------------------------ */

int hjs_has_work(Engine *e) {
    if (e->timers) return 1;
    if (e->ops) return 1;
    return 0; /* microtasks drained in pump; timers/ops checked above */
}

int hjs_pending(Engine *e) { return !e->done || hjs_has_work(e); }

void hjs_mark_done(Engine *e) {
    if (!e->ops && !e->timers && 1) {
        e->done = 1;
    }
}

/* Run due timers + pending jobs. budget_ms: max wall time to spend
 * advancing the virtual clock when no jobs are due. Returns 1 if work
 * may still be outstanding. */
int hjs_pump(Engine *e, int budget_ms) {
    int64_t deadline = now_ms(e) + budget_ms;
    int ran = 0;
    while (now_ms(e) < deadline) {
        if (e->ops) return 1; /* waiting on host HTTP; host drives */
        /* Fire due timers. */
        Timer *t = pop_due_timer(e);
        if (t) {
            ran = 1;
            JSValueConst args[0];
            JSValue ret = JS_Call(e->ctx, t->func, JS_UNDEFINED, 0, args);
            if (JS_IsException(ret)) {
                JSValue ex = JS_GetException(e->ctx);
                const char *msg = JS_ToCString(e->ctx, ex);
                fprintf(stderr, "[hjs] timer exception: %s\n", msg ? msg : "?");
                if (msg) JS_FreeCString(e->ctx, msg);
                JS_FreeValue(e->ctx, ex);
            } else {
                JS_FreeValue(e->ctx, ret);
            }
            if (t->interval_ms > 0) {
                t->when_ms = now_ms(e) + t->interval_ms;
                t->next = e->timers;
                e->timers = t;
            } else {
                free_timer(e, t);
            }
            continue;
        }
        /* Drain microtasks. */
        int more = 1;
        while (more) {
            JSContext *ctx1;
            int err = JS_ExecutePendingJob(e->rt, &ctx1);
            if (err <= 0) { more = 0; }
            else {
                ran = 1;
                if (0) {
                    JSValue ex = JS_GetException(ctx1);
                    const char *msg = JS_ToCString(ctx1, ex);
                    fprintf(stderr, "[hjs] job exception: %s\n", msg ? msg : "?");
                    if (msg) JS_FreeCString(ctx1, msg);
                    JS_FreeValue(ctx1, ex);
                }
            }
        }
        if (e->ops) return 1;
        if (e->timers) {
            /* Advance virtual clock to the earliest timer. */
            int64_t earliest = -1;
            for (Timer *t2 = e->timers; t2; t2 = t2->next) {
                if (earliest < 0 || t2->when_ms < earliest) earliest = t2->when_ms;
            }
            if (earliest > deadline) earliest = deadline;
            if (earliest > now_ms(e)) e->clock_ms = earliest;
            continue;
        }
        break;
    }
    (void)ran;
    return hjs_has_work(e);
}

/* ------------------------------------------------------------------ */
/* Eval + result                                                       */
/* ------------------------------------------------------------------ */

int hjs_eval(Engine *e, const char *code, size_t len) {
    JSValue ret = JS_Eval(e->ctx, code, len, "<page>", JS_EVAL_TYPE_GLOBAL);
    int rc = 0;
    if (JS_IsException(ret)) {
        rc = 1;
        JSValue ex = JS_GetException(e->ctx);
        const char *msg = JS_ToCString(e->ctx, ex);
        fprintf(stderr, "[hjs] eval exception: %s\n", msg ? msg : "?");
        if (msg) JS_FreeCString(e->ctx, msg);
        JS_FreeValue(e->ctx, ex);
    } else {
        JS_FreeValue(e->ctx, ret);
    }
    /* Scripts often end by scheduling timers; run what's runnable now. */
    hjs_pump(e, 0);
    return rc;
}

int hjs_eval_get(Engine *e, const char *code, size_t len) {
    JSValue ret = JS_Eval(e->ctx, code, len, "<extract>", JS_EVAL_TYPE_GLOBAL);
    int rc = 0;
    if (JS_IsException(ret)) {
        rc = 1;
        JSValue ex = JS_GetException(e->ctx);
        const char *msg = JS_ToCString(e->ctx, ex);
        fprintf(stderr, "[hjs] extract exception: %s\n", msg ? msg : "?");
        if (msg) JS_FreeCString(e->ctx, msg);
        JS_FreeValue(e->ctx, ex);
        if (e->result) { free(e->result); e->result = NULL; e->result_len = 0; }
        return rc;
    }
    size_t out_len = 0;
    const char *s = JS_ToCStringLen(e->ctx, &out_len, ret);
    if (e->result) { free(e->result); e->result = NULL; }
    if (s) {
        e->result = malloc(out_len + 1);
        memcpy(e->result, s, out_len);
        e->result[out_len] = 0;
        e->result_len = out_len;
        JS_FreeCString(e->ctx, s);
    }
    JS_FreeValue(e->ctx, ret);
    hjs_pump(e, 0);
    return rc;
}

const char *hjs_result(Engine *e, size_t *out_len) {
    if (out_len) *out_len = e->result_len;
    return e->result ? e->result : "";
}

/* Resolve/reject an http op from the host. status_code: HTTP status;
 * body: response body bytes. The promise value is a JSON string. */
int hjs_resolve_http(Engine *e, int op_id, int status_code,
                     const char *body, size_t body_len) {
    HttpOp **pp = &e->ops;
    HttpOp *found = NULL;
    while (*pp) {
        if ((*pp)->id == op_id) { found = *pp; *pp = found->next; break; }
        pp = &(*pp)->next;
    }
    if (!found) return -1;
    /* Build {"status":N,"body":"..."} as a JS object and stringify,
     * letting QuickJS handle JSON escaping. */
    JSValue obj = JS_NewObject(e->ctx);
    JS_SetPropertyStr(e->ctx, obj, "status", JS_NewInt32(e->ctx, status_code));
    JS_SetPropertyStr(e->ctx, obj, "body",
                      JS_NewStringLen(e->ctx, body ? body : "", body_len));
    JSValue json = JS_JSONStringify(e->ctx, obj, JS_UNDEFINED, JS_UNDEFINED);
    JS_FreeValue(e->ctx, obj);
    if (JS_IsException(json)) {
        JSValue ex = JS_GetException(e->ctx);
        JS_Throw(e->ctx, JS_DupValue(e->ctx, ex));
        JS_FreeValue(e->ctx, ex);
        JS_Call(e->ctx, found->reject_owned, JS_UNDEFINED, 0, NULL);
    } else {
        JSValueConst args[1];
        args[0] = json;
        JS_Call(e->ctx, found->resolve_owned, JS_UNDEFINED, 1, args);
        JS_FreeValue(e->ctx, json);
    }
    JS_FreeValue(e->ctx, found->resolve_owned);
    JS_FreeValue(e->ctx, found->reject_owned);
    free(found);
    return 0;
}

/* List pending http op ids as a JSON array string: [1,2,3].
 * Caller frees via hjs_free_string. Method/url travel with the response
 * as promise metadata is kept host-side by the Mojo layer instead. */
char *hjs_pending_http(Engine *e, int *out_len) {
    int n = 0;
    for (HttpOp *op = e->ops; op; op = op->next) n++;
    char *buf = malloc(16 * (n + 3));
    int off = 0;
    off += snprintf(buf + off, 16, "[");
    for (HttpOp *op = e->ops; op; op = op->next) {
        off += snprintf(buf + off, 16, "%s%d", off > 1 ? "," : "", op->id);
    }
    off += snprintf(buf + off, 16, "]");
    if (out_len) *out_len = off;
    return buf;
}

/* Read method/url/body of a pending op. field: 0=method, 1=url, 2=body.
 * Returns a malloc'd string (freed via hjs_free_string) or NULL. */
char *hjs_op_meta(Engine *e, int op_id, int field) {
    for (HttpOp *op = e->ops; op; op = op->next) {
        if (op->id == op_id) {
            const char *src = NULL;
            if (field == 0) src = op->method;
            else if (field == 1) src = op->url;
            else if (field == 2) src = op->body;
            if (!src) return NULL;
            return strdup(src);
        }
    }
    return NULL;
}


/* Return just the pointer to the last result string (may be NULL). */
const char *hjs_result_ptr(Engine *e) {
    return e->result;
}

/* Return just the pointer to the pending op ids JSON (malloc'd). */
const char *hjs_pending_http_str(Engine *e) {
    int n = 0;
    for (HttpOp *op = e->ops; op; op = op->next) n++;
    char *buf = malloc(16 * (n + 3));
    int off = 0;
    off += snprintf(buf + off, 16, "[");
    for (HttpOp *op = e->ops; op; op = op->next) {
        off += snprintf(buf + off, 16, "%s%d", off > 1 ? "," : "", op->id);
    }
    off += snprintf(buf + off, 16, "]");
    return buf;
}

void hjs_free_string(Engine *e, char *s) { (void)e; free(s); }

/* ------------------------------------------------------------------ */
/* Lifecycle                                                           */
/* ------------------------------------------------------------------ */

void *hjs_new(void) {
    Engine *e = calloc(1, sizeof(Engine));
    e->rt = JS_NewRuntime();
    e->ctx = JS_NewContext(e->rt);
    JS_SetContextOpaque(e->ctx, e);
    JS_SetMemoryLimit(e->rt, 64 * 1024 * 1024);
    JS_SetMaxStackSize(e->rt, 512 * 1024);

    JSValue global = JS_GetGlobalObject(e->ctx);
    JS_SetPropertyStr(e->ctx, global, "setTimeout",
                      JS_NewCFunction(e->ctx, js_set_timeout, "setTimeout", 2));
    JS_SetPropertyStr(e->ctx, global, "setInterval",
                      JS_NewCFunction(e->ctx, js_set_interval, "setInterval", 2));
    JS_SetPropertyStr(e->ctx, global, "clearTimeout",
                      JS_NewCFunction(e->ctx, js_clear_timer, "clearTimeout", 1));
    JS_SetPropertyStr(e->ctx, global, "clearInterval",
                      JS_NewCFunction(e->ctx, js_clear_timer, "clearInterval", 1));
    JS_SetPropertyStr(e->ctx, global, "__hjs_http",
                      JS_NewCFunction(e->ctx, js_http, "__hjs_http", 4));
    JSValue console = JS_NewObject(e->ctx);
    JS_SetPropertyStr(e->ctx, console, "log",
                      JS_NewCFunction(e->ctx, js_log, "log", 1));
    JS_SetPropertyStr(e->ctx, global, "console", console);
    JS_FreeValue(e->ctx, global);
    return e;
}

void hjs_free(Engine *e) {
    if (!e) return;
    while (e->timers) { Timer *t = e->timers; e->timers = t->next; free_timer(e, t); }
    while (e->ops) {
        HttpOp *o = e->ops; e->ops = o->next;
        JS_FreeValue(e->ctx, o->resolve_owned);
        JS_FreeValue(e->ctx, o->reject_owned);
        free(o->method); free(o->url); free(o->body);
        free(o);
    }
    if (e->result) free(e->result);
    JS_FreeContext(e->ctx);
    JS_FreeRuntime(e->rt);
    free(e);
}
