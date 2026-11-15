"use client";
import { Mic } from "lucide-react";
import { Badge } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import type { VoiceMessageMeta } from "@/lib/types";

export function VoiceNote({ message, lowConfidenceThreshold }: { message: { message_type: string; text: string; meta?: VoiceMessageMeta | null }; lowConfidenceThreshold?: number }) {
  const { t } = useI18n();
  const meta = message.meta;
  const voice = ["audio", "voice", "voice_note"].includes(message.message_type) || (message.message_type !== "voice_turn" && !!meta && ("transcribed" in meta || "stt" in meta || "stt_error" in meta));
  if (!voice) return <p className="whitespace-pre-wrap break-words">{message.text}</p>;
  const failed = meta?.transcribed === false || !!meta?.stt_error;
  const confidence = meta?.stt_confidence;
  const validConfidence = typeof confidence === "number" && Number.isFinite(confidence) && confidence >= 0 && confidence <= 1;
  const low = meta?.stt_low_confidence === true || (validConfidence && typeof lowConfidenceThreshold === "number" && confidence < lowConfidenceThreshold);
  return <div className="space-y-2" data-testid="voice-note">
    <Badge><Mic size={13} aria-hidden="true"/>{t("Голосовое сообщение", "Дауыстық хабарлама")}</Badge>
    {failed ? <p className="muted text-sm">{t("Не распознано", "Танылмады")}</p> : <>
      <p className="whitespace-pre-wrap break-words">{message.text}</p>
      <p className={`text-xs ${low ? "text-amber-800 dark:text-amber-300" : "muted"}`}>{low ? t("Низкая точность", "Төмен дәлдік") : t("Точность распознавания", "Тану дәлдігі")}{validConfidence ? `: ${Math.round(confidence * 100)}%` : !low ? `: ${t("Неизвестна", "Белгісіз")}` : ""}</p>
    </>}
  </div>;
}
