// 市場時計・セッション判定はすべて JST 基準。
//
// ⚠️ Date.getHours()/getMinutes() はブラウザ/実行ホストのローカル
// タイムゾーンを使う。「JST」と明示ラベルする画面がこれらを直接使うと、
// ホストのタイムゾーン設定 (UTC 設定の CI ランナー、海外設定の端末) 次第で
// 表示もセッション判定もずれる (Codex レビューで実測: AlmanacStrip が
// UTC ランナー上で "23:15 JST" 相当の瞬間を "14:15" と表示していた)。
// 日本は DST が無いので UTC+9 固定オフセットとして扱ってよい。

export function jstMinutesOfDay(date: Date): number {
  const jst = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return jst.getUTCHours() * 60 + jst.getUTCMinutes()
}

export function jstHHMM(date: Date): string {
  const jst = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return `${String(jst.getUTCHours()).padStart(2, '0')}:${String(jst.getUTCMinutes()).padStart(2, '0')}`
}

export function jstMonthDay(date: Date): { month: number; date: number } {
  const jst = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return { month: jst.getUTCMonth() + 1, date: jst.getUTCDate() }
}
