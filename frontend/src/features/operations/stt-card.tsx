"use client";
import { useState } from "react";
import { Mic, RefreshCw } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useApi } from "@/hooks/use-api";
import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import type { STTIntegration, STTTestResult } from "@/lib/types";
import { DataState } from "./shared";

export function SttCard() {
  const { t } = useI18n();
  const { can } = useSession();
  const query = useApi<STTIntegration>("/v1/integrations/stt");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<STTTestResult | null>(null);
  const [error, setError] = useState(false);
  return <Card className="mt-6 space-y-4" data-testid="stt-card">
    <h2 className="flex items-center gap-2 text-lg font-semibold"><Mic size={22} aria-hidden="true"/>{t("Распознавание речи", "Сөйлеуді тану")}</h2>
    <DataState query={query}>{query.data ? <>
      <Badge variant={query.data.configured ? "success" : "warning"}>{query.data.configured ? t("Настроено", "Бапталған") : t("Не настроено", "Бапталмаған")}</Badge>
      <dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="muted">{t("Провайдер", "Провайдер")}</dt><dd className="break-words">{query.data.provider || "—"}</dd></div><div><dt className="muted">{t("Модель", "Модель")}</dt><dd className="break-words">{query.data.model || t("Не задано", "Берілмеген")}</dd></div></dl>
      {can("integrations.write") && <Button disabled={busy} onClick={async () => {
        setBusy(true); setResult(null); setError(false);
        try { setResult(await api<STTTestResult>("/v1/integrations/stt/test", { method: "POST" })); }
        catch { setError(true); } finally { setBusy(false); }
      }}><RefreshCw size={15} className={busy ? "animate-spin" : ""}/>{busy ? t("Проверяем распознавание…", "Тануды тексерудеміз…") : t("Проверить распознавание", "Тануды тексеру")}</Button>}
      {result && <p role={result.ok ? "status" : "alert"} className={result.ok ? "text-sm" : "text-sm text-red-600 dark:text-red-300"}>{result.ok ? t("Распознавание работает", "Тану жұмыс істейді") : t("Ошибка распознавания", "Тану қатесі")} · {result.provider} · {result.latency_ms} {t("мс", "мс")}</p>}
      {error && <p role="alert">{t("Не удалось проверить распознавание. Повторите попытку.", "Тануды тексеру мүмкін болмады. Қайталап көріңіз.")}</p>}
    </> : <p className="muted">{t("Нет данных о распознавании", "Тану туралы деректер жоқ")}</p>}</DataState>
  </Card>;
}
