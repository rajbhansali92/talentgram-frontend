import React, { useEffect, useState } from "react";
import { MessageCircle, CheckCircle, AlertTriangle, Clock } from "lucide-react";
import { getTimeline } from "@/lib/whatsappApi";

/**
 * Unified communication timeline (Feature 2 / Slice 4).
 * Renders WhatsApp + other touchpoints for ANY subject — talent, CRM contact,
 * or future client — from the single /whatsapp/timeline endpoint.
 *
 * Usage:
 *   <CommTimeline subjectType="TALENT" subjectId={talentId} />
 *   <CommTimeline subjectType="CRM_CLIENT" subjectId={clientId} />
 */
const STATUS_META = {
  sent: { label: "Delivered", color: "text-emerald-700 bg-emerald-50", Icon: CheckCircle },
  delivered: { label: "Delivered", color: "text-emerald-700 bg-emerald-50", Icon: CheckCircle },
  sent_unverified: { label: "Sent (Unconfirmed)", color: "text-orange-700 bg-orange-50", Icon: AlertTriangle },
  failed: { label: "Failed", color: "text-red-700 bg-red-50", Icon: AlertTriangle },
};

function fmtDate(d) {
  if (!d) return "";
  try {
    return new Date(d).toLocaleString("en-US", {
      day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return String(d);
  }
}

export default function CommTimeline({ subjectType, subjectId, title = "Communication Timeline" }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    if (!subjectType || !subjectId) return;
    setLoading(true);
    setError(false);
    getTimeline(subjectType, subjectId, { limit: 50 })
      .then((data) => { if (alive) setItems(data.items || []); })
      .catch(() => { if (alive) setError(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [subjectType, subjectId]);

  return (
    <div className="space-y-3" data-testid="comm-timeline">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-black/45">{title}</h3>

      {loading && <p className="text-xs text-black/40">Loading timeline…</p>}
      {error && <p className="text-xs text-red-600">Unable to load communication history.</p>}
      {!loading && !error && items.length === 0 && (
        <p className="text-xs text-black/40">No communications recorded yet.</p>
      )}

      <ol className="space-y-3">
        {items.map((it) => {
          const meta = STATUS_META[it.status] || { label: it.status || "Sent", color: "text-black/60 bg-black/5", Icon: Clock };
          const { Icon } = meta;
          return (
            <li key={it.id} className="flex gap-3 border-l-2 border-black/10 pl-3">
              <div className="shrink-0 mt-0.5">
                <MessageCircle className="w-4 h-4 text-black/40" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs font-semibold text-black/80">
                    WhatsApp{it.template_name ? ` · ${it.template_name}` : ""}
                  </span>
                  <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded-sm inline-flex items-center gap-1 ${meta.color}`}>
                    <Icon className="w-3 h-3" />{meta.label}
                  </span>
                </div>
                {it.preview && (
                  <p className="text-[11px] text-black/55 mt-0.5 truncate" title={it.preview}>{it.preview}</p>
                )}
                <p className="text-[10px] text-black/35 mt-0.5 font-mono">{fmtDate(it.created_at)}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
