"use client";

import { useState } from "react";
import { PhoneCall } from "lucide-react";
import { Button, Card } from "@/components/ui";
import { useApi, useAction } from "@/hooks/use-api";
import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { DataState, Editor, type Field } from "./shared";
import { updateVoiceSettings, voiceThresholdPercent } from "./voice-settings-values";

type Tenant = { settings: Record<string, unknown> };
export function VoiceSettings() {
  const { t } = useI18n();
  const { can } = useSession();
  const tenant = useApi<Tenant>("/v1/tenant");
  const action = useAction();
  const [editing, setEditing] = useState(false);
  const voice = (tenant.data?.settings.voice ?? {}) as Record<string, unknown>;
  const displayedVoice: Record<string, unknown> = { ...voice, low_confidence_threshold: voiceThresholdPercent(voice.low_confidence_threshold) };
  const fields: Field[] = [
    { key: "recording_enabled", ru: "Запись разговоров", kk: "Қоңырауларды жазу", type: "checkbox" },
    { key: "transcript_retention_days", ru: "Хранение расшифровок, дней", kk: "Транскриптті сақтау, күн", type: "number", min: 1, required: true },
    { key: "max_outbound_per_day", ru: "Максимум исходящих звонков в день", kk: "Күніне шығыс қоңыраулардың шегі", type: "number", min: 0, required: true },
    { key: "low_confidence_threshold", ru: "Порог уверенности распознавания, %", kk: "Сөйлеуді тану сенімділігінің шегі, %", type: "number", min: 0, max: 100, step: 1, required: true },
    { key: "tts_voice_ru", ru: "Голос синтеза речи RU", kk: "RU сөйлеу синтезінің дауысы" },
    { key: "tts_voice_kk", ru: "Голос синтеза речи KK", kk: "KK сөйлеу синтезінің дауысы" },
  ];
  return <Card className="mt-6">
    <div className="mb-4 flex items-center justify-between gap-3">
      <h2 className="flex items-center gap-2 font-semibold"><PhoneCall size={20}/>{t("Голосовой канал", "Дауыстық арна")}</h2>
      {can("settings.write") && tenant.data && <Button onClick={() => setEditing(true)}>{t("Настроить голосовой канал", "Дауыстық арнаны баптау")}</Button>}
    </div>
    <DataState query={tenant}>
      <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">{fields.map(field => <div key={field.key}>
        <dt className="muted">{t(field.ru, field.kk)}</dt>
        <dd className="mt-1 font-medium">{displayedVoice[field.key] === undefined ? t("Не задано", "Берілмеген") : field.type === "checkbox" ? (displayedVoice[field.key] ? t("Включено", "Қосылған") : t("Выключено", "Өшірілген")) : `${String(displayedVoice[field.key]) || "—"}${field.key === "low_confidence_threshold" ? "%" : ""}`}</dd>
      </div>)}</dl>
    </DataState>
    {editing && <Editor title={t("Голосовой канал", "Дауыстық арна")} fields={fields} initial={displayedVoice} onClose={() => setEditing(false)} onSave={async values => {
      // Read immediately before patching because PATCH replaces settings.voice as a whole.
      const latest = await api<Tenant>("/v1/tenant");
      await action.run("/v1/tenant", { settings: updateVoiceSettings(latest.settings, values) }, "PATCH");
      await tenant.refetch();
    }}/>}
  </Card>;
}
