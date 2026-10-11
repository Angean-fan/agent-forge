/** 把后端返回的 UTC 时间串按本地时区展示。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  const normalized = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const pad = (n: number): string => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 微单位金额（1 元 = 1_000_000 微）→ 元。 */
export function formatMicroYuan(micro: number): string {
  return `¥${(micro / 1_000_000).toFixed(4)}`;
}
