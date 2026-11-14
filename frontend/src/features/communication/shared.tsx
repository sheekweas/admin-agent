"use client";

import { Button, Card, EmptyState, Skeleton } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { ReactNode } from "react";

export type Collection<T> = { items: T[]; total: number };
export type Person = { id: string; name: string; phone_e164: string };
export function items<T>(value: Collection<T> | T[] | undefined): T[] {
  return Array.isArray(value) ? value : value?.items ?? [];
}
export function DataState({ loading, error, empty, retry, children }: { loading: boolean; error: unknown; empty?: boolean; retry: () => unknown; children: ReactNode }) {
  const { t } = useI18n();
  if (loading) return <div className="space-y-3 p-5"><Skeleton /><Skeleton /><Skeleton /></div>;
  if (error instanceof ApiError && error.status === 403) return <EmptyState title={t("Нет доступа", "Қолжетімсіз")} description={t("У вас нет разрешения на просмотр этого раздела.", "Бұл бөлімді көруге рұқсатыңыз жоқ.")} />;
  if (error) return <Card className="!p-0"><div className="space-y-4 p-4" role="alert"><p>{error instanceof Error ? error.message : t("Не удалось загрузить данные", "Деректерді жүктеу мүмкін болмады")}</p><Button onClick={() => void retry()}>{t("Повторить", "Қайталау")}</Button></div></Card>;
  if (empty) return <EmptyState title={t("Пока ничего нет", "Әзірге ештеңе жоқ")} description={t("Новые данные появятся здесь автоматически.", "Жаңа деректер осында автоматты түрде пайда болады.")} />;
  return <>{children}</>;
}


