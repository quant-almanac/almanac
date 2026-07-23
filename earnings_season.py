from datetime import datetime

def get_earnings_season():
    """現在が決算シーズン中かを判定"""
    month = datetime.now().month
    # 1月（Q3決算）・4月（Q4決算）・7月（Q1決算）・10月（Q2決算）
    # 各月の1〜31日を決算シーズンとする
    if month in [1, 4, 7, 10]:
        week = (datetime.now().day - 1) // 7 + 1
        if week <= 4:  # 月の前半4週間
            return True, f"{month}月決算シーズン"
    return False, "通常期"

def get_season_config():
    """決算シーズンに応じた設定を返す"""
    in_season, label = get_earnings_season()
    if in_season:
        return {
            'in_season': True,
            'label': label,
            'signal_threshold_delta': -0.3,  # 閾値を0.3下げる（積極的に）
            'event_driven_priority': True,    # イベントドリブンを最優先
            'analysis_note': f'📅 {label}中：イベントドリブン戦略を優先します'
        }
    return {
        'in_season': False,
        'label': '通常期',
        'signal_threshold_delta': 0,
        'event_driven_priority': False,
        'analysis_note': ''
    }

if __name__ == "__main__":
    config = get_season_config()
    print(f"決算シーズン: {config['label']}")
    print(f"閾値調整: {config['signal_threshold_delta']:+.1f}")
