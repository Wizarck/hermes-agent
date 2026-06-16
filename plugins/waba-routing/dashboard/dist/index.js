/* waba-routing — Hermes dashboard plugin.
 *
 * A console over the waba-mcp routing registry (H multi-number): receiving
 * "lines" (number → desk role/tier/target) and sender "grants" (which roles a
 * person may assume). All writes go through the plugin backend → the waba-mcp
 * admin API, which validates + audits. Plain IIFE; React comes from the SDK.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components;

  var FIELD =
    "rounded border border-border bg-background px-2 py-1 text-sm w-full";
  var BASE = "/api/plugins/waba-routing";

  function api(path, init) {
    return SDK.fetchJSON(BASE + path, init);
  }
  function postJSON(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  // SDK.fetchJSON throws `new Error("<status>: <body>")` on non-2xx; the body is
  // our JSON error envelope. Pull the human message out of it.
  function cleanErr(e) {
    var msg = String((e && e.message) || e || "");
    var i = msg.indexOf(": ");
    if (i > 0) {
      try {
        var parsed = JSON.parse(msg.slice(i + 2));
        if (parsed && parsed.error) return parsed.error;
      } catch (_) {}
    }
    return msg;
  }

  // --- small UI atoms -------------------------------------------------------

  function Field(props) {
    return h(
      "label",
      { className: "flex flex-col gap-1 text-xs text-muted-foreground" },
      props.label,
      props.children
    );
  }

  function Select(props) {
    return h(
      "select",
      {
        className: FIELD,
        value: props.value || "",
        onChange: function (e) {
          props.onChange(e.target.value);
        },
      },
      (props.empty ? [h("option", { key: "_", value: "" }, props.empty)] : []).concat(
        props.options.map(function (o) {
          return h("option", { key: o, value: o }, o);
        })
      )
    );
  }

  // --- lines ----------------------------------------------------------------

  function LineRow(props) {
    var line = props.line;
    var roles = props.roles;
    var stub = !line.enabled && !line.default_role;
    var s = useState({
      label: line.label || "",
      default_role: line.default_role || "",
      tier: line.tier || "normal",
      forward_target: line.forward_target || "hermes",
      enabled: !!line.enabled,
    });
    var form = s[0];
    var setForm = s[1];
    function up(k, v) {
      setForm(Object.assign({}, form, { [k]: v }));
    }
    function save() {
      props.onSave(Object.assign({ phone_number_id: line.phone_number_id }, form));
    }

    return h(
      C.Card,
      { className: stub ? "border-dashed" : "" },
      h(
        C.CardContent,
        { className: "py-3 space-y-2" },
        h(
          "div",
          { className: "flex items-center justify-between gap-2" },
          h(
            "div",
            { className: "text-sm font-medium" },
            line.display_number || line.phone_number_id,
            stub
              ? h(C.Badge, { className: "ml-2", variant: "outline" }, "sin configurar")
              : line.enabled
              ? h(C.Badge, { className: "ml-2" }, "activa")
              : h(C.Badge, { className: "ml-2", variant: "outline" }, "desactivada")
          ),
          h(
            "code",
            { className: "text-[10px] text-muted-foreground" },
            line.phone_number_id
          )
        ),
        h(
          "div",
          { className: "grid grid-cols-2 md:grid-cols-5 gap-2" },
          h(
            Field,
            { label: "Etiqueta" },
            h("input", {
              className: FIELD,
              value: form.label,
              onChange: function (e) {
                up("label", e.target.value);
              },
            })
          ),
          h(
            Field,
            { label: "Rol por defecto" },
            h(Select, {
              value: form.default_role,
              empty: "—",
              options: roles,
              onChange: function (v) {
                up("default_role", v);
              },
            })
          ),
          h(
            Field,
            { label: "Tier" },
            h(Select, {
              value: form.tier,
              options: ["normal", "critical"],
              onChange: function (v) {
                up("tier", v);
              },
            })
          ),
          h(
            Field,
            { label: "Destino" },
            h(Select, {
              value: form.forward_target,
              options: ["hermes", "wordpress"],
              onChange: function (v) {
                up("forward_target", v);
              },
            })
          ),
          h(
            Field,
            { label: "Activa" },
            h(
              "label",
              { className: "flex items-center gap-2 text-sm h-[30px]" },
              h("input", {
                type: "checkbox",
                checked: form.enabled,
                onChange: function (e) {
                  up("enabled", e.target.checked);
                },
              }),
              form.enabled ? "sí" : "no"
            )
          )
        ),
        h(
          "div",
          { className: "flex gap-2 justify-end" },
          h(
            C.Button,
            { size: "sm", onClick: save, disabled: props.busy },
            "Guardar"
          ),
          h(
            C.Button,
            {
              size: "sm",
              variant: "destructive",
              onClick: function () {
                props.onDelete(line.phone_number_id);
              },
              disabled: props.busy,
            },
            "Borrar"
          )
        )
      )
    );
  }

  function LinesCard(props) {
    return h(
      C.Card,
      null,
      h(
        C.CardHeader,
        null,
        h(C.CardTitle, null, "Líneas (números receptores)"),
        h(
          "p",
          { className: "text-xs text-muted-foreground" },
          "Cada número que escribe al negocio aparece aquí automáticamente como ",
          h("em", null, "sin configurar"),
          ". Asígnale rol, tier y destino, y actívala."
        )
      ),
      h(
        C.CardContent,
        { className: "space-y-3" },
        props.lines.length === 0
          ? h(
              "p",
              { className: "text-sm text-muted-foreground" },
              "Aún no se ha visto ningún número. En cuanto llegue un mensaje a un número, aparecerá aquí."
            )
          : props.lines.map(function (l) {
              return h(LineRow, {
                key: l.phone_number_id,
                line: l,
                roles: props.roles,
                onSave: props.onSave,
                onDelete: props.onDelete,
                busy: props.busy,
              });
            })
      )
    );
  }

  // --- grants ---------------------------------------------------------------

  function AddGrant(props) {
    var roles = props.roles;
    var lines = props.lines;
    var s = useState({ phone: "", allowed: [], default_role: "", line_id: "" });
    var form = s[0];
    var setForm = s[1];
    function toggleRole(r) {
      var has = form.allowed.indexOf(r) >= 0;
      var allowed = has
        ? form.allowed.filter(function (x) {
            return x !== r;
          })
        : form.allowed.concat([r]);
      setForm(Object.assign({}, form, { allowed: allowed }));
    }
    function submit() {
      props.onSave({
        phone: form.phone,
        allowed_roles: form.allowed,
        default_role: form.default_role || undefined,
        line_id: form.line_id || "",
      });
      setForm({ phone: "", allowed: [], default_role: "", line_id: "" });
    }
    var lineOptions = lines.map(function (l) {
      return l.phone_number_id;
    });
    return h(
      "div",
      { className: "rounded border border-dashed border-border p-3 space-y-2" },
      h("div", { className: "text-sm font-medium" }, "Nuevo permiso"),
      h(
        "div",
        { className: "grid grid-cols-2 md:grid-cols-4 gap-2" },
        h(
          Field,
          { label: "Teléfono (E.164)" },
          h("input", {
            className: FIELD,
            placeholder: "34600100200",
            value: form.phone,
            onChange: function (e) {
              setForm(Object.assign({}, form, { phone: e.target.value }));
            },
          })
        ),
        h(
          Field,
          { label: "Por defecto" },
          h(Select, {
            value: form.default_role,
            empty: "(de la línea)",
            options: form.allowed,
            onChange: function (v) {
              setForm(Object.assign({}, form, { default_role: v }));
            },
          })
        ),
        h(
          Field,
          { label: "Línea (opcional)" },
          h(Select, {
            value: form.line_id,
            empty: "global",
            options: lineOptions,
            onChange: function (v) {
              setForm(Object.assign({}, form, { line_id: v }));
            },
          })
        ),
        h(
          Field,
          { label: "Roles permitidos" },
          h(
            "div",
            { className: "flex flex-wrap gap-2 pt-1" },
            roles.map(function (r) {
              return h(
                "label",
                { key: r, className: "flex items-center gap-1 text-xs" },
                h("input", {
                  type: "checkbox",
                  checked: form.allowed.indexOf(r) >= 0,
                  onChange: function () {
                    toggleRole(r);
                  },
                }),
                r
              );
            })
          )
        )
      ),
      h(
        "div",
        { className: "flex justify-end" },
        h(
          C.Button,
          {
            size: "sm",
            onClick: submit,
            disabled: props.busy || !form.phone || form.allowed.length === 0,
          },
          "Añadir permiso"
        )
      )
    );
  }

  function GrantRow(props) {
    var g = props.grant;
    return h(
      "div",
      {
        className:
          "flex items-center justify-between gap-2 rounded border border-border px-3 py-2",
      },
      h(
        "div",
        { className: "space-y-1" },
        h(
          "div",
          { className: "text-sm font-medium" },
          g.phone,
          h(
            C.Badge,
            { className: "ml-2", variant: "outline" },
            g.line_id ? "línea " + g.line_id : "global"
          ),
          g.enabled ? null : h(C.Badge, { className: "ml-2", variant: "outline" }, "off")
        ),
        h(
          "div",
          { className: "flex flex-wrap gap-1" },
          (g.allowed_roles || []).map(function (r) {
            return h(
              C.Badge,
              { key: r, variant: r === g.default_role ? "default" : "secondary" },
              r
            );
          })
        )
      ),
      h(
        C.Button,
        {
          size: "sm",
          variant: "destructive",
          disabled: props.busy,
          onClick: function () {
            props.onDelete(g.phone, g.line_id || "");
          },
        },
        "Borrar"
      )
    );
  }

  function GrantsCard(props) {
    return h(
      C.Card,
      null,
      h(
        C.CardHeader,
        null,
        h(C.CardTitle, null, "Permisos por persona"),
        h(
          "p",
          { className: "text-xs text-muted-foreground" },
          "Qué roles puede asumir cada teléfono. Un permiso de línea pisa al global. Sin permiso, la persona usa el rol por defecto de la línea."
        )
      ),
      h(
        C.CardContent,
        { className: "space-y-3" },
        h(AddGrant, {
          roles: props.roles,
          lines: props.lines,
          onSave: props.onSave,
          busy: props.busy,
        }),
        props.grants.length === 0
          ? h("p", { className: "text-sm text-muted-foreground" }, "Sin permisos. Las personas usan el rol por defecto de su línea.")
          : props.grants.map(function (g) {
              return h(GrantRow, {
                key: g.phone + "|" + (g.line_id || ""),
                grant: g,
                onDelete: props.onDelete,
                busy: props.busy,
              });
            })
      )
    );
  }

  // --- audit ----------------------------------------------------------------

  function AuditCard(props) {
    if (!props.audit || props.audit.length === 0) return null;
    return h(
      C.Card,
      null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Cambios recientes")),
      h(
        C.CardContent,
        { className: "space-y-1" },
        props.audit.map(function (a) {
          return h(
            "div",
            {
              key: a.id,
              className: "flex items-center justify-between text-xs text-muted-foreground",
            },
            h("span", null, a.action + " · " + a.target),
            h("span", null, (a.actor || "") + " · " + SDK.utils.isoTimeAgo(a.created_at))
          );
        })
      )
    );
  }

  function NotConfigured() {
    return h(
      C.Card,
      null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Routing — sin configurar")),
      h(
        C.CardContent,
        { className: "text-sm text-muted-foreground space-y-2" },
        h(
          "p",
          null,
          "Define ",
          h("code", null, "WABA_ADMIN_URL"),
          " y ",
          h("code", null, "WABA_ADMIN_API_TOKEN"),
          " en el entorno del dashboard para conectar con la API admin de waba-mcp."
        )
      )
    );
  }

  // --- root -----------------------------------------------------------------

  function RoutingPage() {
    var sConf = useState(true);
    var sRoles = useState([]);
    var sLines = useState([]);
    var sGrants = useState([]);
    var sAudit = useState([]);
    var sErr = useState("");
    var sBusy = useState(false);
    var configured = sConf[0];
    var roles = sRoles[0];

    var reload = useCallback(function () {
      sErr[1]("");
      return api("/health")
        .then(function (hres) {
          sConf[1](!!hres.configured);
          if (!hres.configured) return null;
          return Promise.all([
            api("/roles"),
            api("/lines"),
            api("/grants"),
            api("/audit?limit=20"),
          ]).then(function (res) {
            sRoles[1](Object.keys((res[0] && res[0].roles) || {}));
            sLines[1]((res[1] && res[1].lines) || []);
            sGrants[1]((res[2] && res[2].grants) || []);
            sAudit[1]((res[3] && res[3].audit) || []);
          });
        })
        .catch(function (e) {
          sErr[1](cleanErr(e));
        });
    }, []);

    useEffect(function () {
      reload();
    }, [reload]);

    function withBusy(p) {
      sBusy[1](true);
      sErr[1]("");
      return p
        .then(function (res) {
          if (res && res.error) sErr[1](res.error);
          return reload();
        })
        .catch(function (e) {
          sErr[1](cleanErr(e));
        })
        .then(function () {
          sBusy[1](false);
        });
    }

    function saveLine(line) {
      return withBusy(postJSON("/lines", line));
    }
    function delLine(id) {
      return withBusy(api("/lines/" + encodeURIComponent(id), { method: "DELETE" }));
    }
    function saveGrant(g) {
      return withBusy(postJSON("/grants", g));
    }
    function delGrant(phone, lineId) {
      return withBusy(
        api(
          "/grants?phone=" +
            encodeURIComponent(phone) +
            "&line_id=" +
            encodeURIComponent(lineId || ""),
          { method: "DELETE" }
        )
      );
    }

    if (!configured) return h(NotConfigured);

    return h(
      "div",
      { className: "space-y-4" },
      sErr[0]
        ? h(
            C.Card,
            null,
            h(C.CardContent, { className: "py-2 text-sm text-destructive" }, sErr[0])
          )
        : null,
      h(LinesCard, {
        lines: sLines[0],
        roles: roles,
        onSave: saveLine,
        onDelete: delLine,
        busy: sBusy[0],
      }),
      h(GrantsCard, {
        grants: sGrants[0],
        lines: sLines[0],
        roles: roles,
        onSave: saveGrant,
        onDelete: delGrant,
        busy: sBusy[0],
      }),
      h(AuditCard, { audit: sAudit[0] })
    );
  }

  window.__HERMES_PLUGINS__.register("waba-routing", RoutingPage);
})();
