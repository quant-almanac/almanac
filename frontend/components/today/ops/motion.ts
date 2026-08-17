/**
 * ALMANAC Ops — 面とモーションの共通レシピ (v8)
 *
 * v7 は全ての面に 上端ハイライト + 天面グラデ + 多重の影 を敷いてベベル調に
 * なっていた。v8 はそれを全て外し、面は「明度差 + 1px の輪郭」だけで表す。
 * 影は本当に浮いているもの（モーダル・ポップオーバー）専用。
 *
 * 動きは 0.15s(反応) / 0.45s(登場) / 2.4s以上(環境) の3階層。
 * prefers-reduced-motion では登場・環境を止め、反応だけ残す。
 */
import { OPS } from './tokens'

export const OPS_MOTION_CSS = `
/* ── 登場 ───────────────────────────────────────────── */
@keyframes opsFadeUp {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: none; }
}
@keyframes opsBackdropIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes opsModalIn {
  from { opacity: 0; transform: translateY(10px) scale(.985); }
  to   { opacity: 1; transform: none; }
}
@keyframes opsBarGrow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@keyframes opsPop { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }

/* ── 環境 ───────────────────────────────────────────── */
@keyframes opsLinkPulse {
  0%, 100% { box-shadow: 0 0 0 0 ${OPS.gold}00; }
  50%      { box-shadow: 0 0 0 3px ${OPS.gold}33; }
}
@keyframes opsDotPulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
@keyframes opsSpin { to { transform: rotate(360deg); } }

.ops-sec { animation: opsFadeUp .45s cubic-bezier(.22,.8,.3,1) both; }
.ops-pop { animation: opsPop .4s cubic-bezier(.22,.8,.3,1) both; }
.ops-bar-fill { transform-origin: left; animation: opsBarGrow .6s cubic-bezier(.3,.9,.3,1) both; }
.ops-linked { animation: opsLinkPulse 1.6s ease-in-out infinite; }
.ops-dot-pulse { animation: opsDotPulse 2.2s ease-in-out infinite; }
.ops-spin { animation: opsSpin 1s linear infinite; }

/* ── 面 ─────────────────────────────────────────────
   グラデも内側ハイライトも使わない。地より明るい単色 + 輪郭だけ。 */
.ops-elev {
  background: ${OPS.panel};
  border: 1px solid ${OPS.border};
}
.ops-elev-2 {
  background: ${OPS.panelAlt};
  border: 1px solid ${OPS.border};
}
/* 溝。メーター・トラックなど「へこんでいる」もの。影ではなく暗さで表す */
.ops-well {
  background: ${OPS.sunken};
  border: 1px solid ${OPS.hairline};
}

/* ── 反応 ───────────────────────────────────────────
   hover は「面が一段明るくなる + 輪郭が金に寄る」。持ち上げない。 */
.ops-card {
  background: ${OPS.panel};
  transition: background .15s ease, border-color .15s ease;
}
.ops-card:hover {
  background: ${OPS.panelAlt};
  border-color: ${OPS.gold}77 !important;
}
.ops-clickable { cursor: pointer; }
.ops-row { transition: background .13s ease, box-shadow .13s ease; }
.ops-row:hover {
  background: ${OPS.raised};
  box-shadow: inset 2px 0 0 ${OPS.gold};
}

/* ボタン: 面の明度で状態を出す。押し込み影は使わない */
.ops-btn {
  border-radius: 6px;
  cursor: pointer;
  transition: background .15s ease, border-color .15s ease, color .15s ease, opacity .15s ease;
}
.ops-btn:hover:not(:disabled) { filter: brightness(1.18); }
.ops-btn:active:not(:disabled) { filter: brightness(.92); }
.ops-btn:disabled { opacity: .55; cursor: wait; }

@media (prefers-reduced-motion: reduce) {
  .ops-sec, .ops-pop, .ops-bar-fill { animation: none; }
  .ops-linked, .ops-dot-pulse, .ops-spin { animation: none; }
  .ops-card, .ops-row, .ops-btn { transition: none; }
}
`
