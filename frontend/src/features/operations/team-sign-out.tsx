"use client";
import { ConfirmButton } from "@/components/ui";
import { api, notify } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";

export function TeamSignOut({ membershipId, name }: { membershipId?: string; name: string }) {
  const { can } = useSession();
  const { t } = useI18n();
  if (!can("team.manage") || !membershipId) return null;
  return <ConfirmButton variant="secondary" title={t("Завершить сеансы пользователя?", "Пайдаланушы сеанстарын аяқтау керек пе?")} description={`${name}. ${t("Пользователю потребуется войти снова.", "Пайдаланушы қайта кіруі керек.")}`} onConfirm={async () => {
    const result = await api<{ revoked: number }>(`/v1/team/${encodeURIComponent(membershipId)}/sign-out`, { method: "POST" });
    notify(`${t("Завершено сеансов", "Аяқталған сеанстар")}: ${result.revoked}`);
  }}>{t("Выйти за пользователя", "Пайдаланушы сеанстарын аяқтау")}</ConfirmButton>;
}
