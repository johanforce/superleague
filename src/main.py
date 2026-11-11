# -*- coding: utf-8 -*-
"""Khởi động Super League Simulator."""
import argparse
import json
import random
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import sl_engine as eng
from sl_game import Game, load_source_database, average_top11, APP_DIR
from sl_database import SuperLeagueDatabase


def run_engine_calibration(matches=2000, seed=None, quiet=False):
    """Hiệu chỉnh M0: bàn/trận và tỷ lệ thắng của đội mạnh nhất trước đội yếu nhất."""
    matches = max(100, int(matches))
    source_db = SuperLeagueDatabase(APP_DIR / "memory" / "super_league.db")
    fields, rows, squads = load_source_database(source_db)
    with open(APP_DIR / "config.json", "r", encoding="utf-8") as f:
        raw_config = json.load(f)
    engine_cfg = raw_config.get("engine", {})
    seed = int(seed if seed is not None else raw_config.get("seed", 20261029)) + 77123
    rng = random.Random(seed)
    names = list(squads)

    total_goals = 0
    for _ in range(matches):
        home = rng.choice(names)
        away = rng.choice([team for team in names if team != home])
        result = eng.simulate_match(home, away, squads[home], squads[away], rng=rng, engine_cfg=engine_cfg)
        total_goals += int(result["hg"]) + int(result["ag"])
    goals_per_match = total_goals / matches

    strength = {team: average_top11(players) for team, players in squads.items()}
    strongest = max(names, key=lambda team: (strength[team], team))
    weakest = min(names, key=lambda team: (strength[team], team))
    wins = draws = losses = 0
    for i in range(matches):
        # Đảo sân để không làm tỷ lệ thắng phụ thuộc vào lợi thế sân nhà.
        home, away = (strongest, weakest) if i % 2 == 0 else (weakest, strongest)
        result = eng.simulate_match(home, away, squads[home], squads[away], rng=rng, engine_cfg=engine_cfg)
        strong_goals, weak_goals = ((result["hg"], result["ag"]) if home == strongest
                                    else (result["ag"], result["hg"]))
        if strong_goals > weak_goals: wins += 1
        elif strong_goals == weak_goals: draws += 1
        else: losses += 1
    win_rate = wins / matches
    passed = 2.3 <= goals_per_match <= 2.8 and 0.60 <= win_rate <= 0.75
    summary = {
        "matches_random": matches,
        "goals_per_match": round(goals_per_match, 3),
        "strongest": strongest,
        "strongest_rating": round(strength[strongest], 2),
        "weakest": weakest,
        "weakest_rating": round(strength[weakest], 2),
        "head_to_head_matches": matches,
        "strong_wins": wins,
        "draws": draws,
        "strong_losses": losses,
        "strong_win_rate": round(win_rate, 4),
        "passed": passed,
    }
    if not quiet:
        print(f"Calibrate engine ({matches} trận ngẫu nhiên): {goals_per_match:.3f} bàn/trận "
              f"({'OK' if 2.3 <= goals_per_match <= 2.8 else 'NGOÀI NGƯỠNG 2.3–2.8'}).")
        print(f"Đối đầu {strongest} ({strength[strongest]:.1f}) - {weakest} ({strength[weakest]:.1f}), "
              f"{matches} trận đảo sân: thắng {wins}, hòa {draws}, thua {losses}; tỷ lệ thắng {win_rate:.1%} "
              f"({'OK' if 0.60 <= win_rate <= 0.75 else 'NGOÀI NGƯỠNG 60–75%'}).")
    return summary


def run_checks():
    source_db = SuperLeagueDatabase(APP_DIR / "memory" / "super_league.db")
    fields, rows, teams = load_source_database(source_db)
    print(f"SQLite catalog OK: {len(rows)} cầu thủ, {len(teams)} CLB, {len(fields)} cột.")
    for team, squad in sorted(teams.items()):
        print(f"  {team}: {len(squad)} cầu thủ")
    with tempfile.TemporaryDirectory(prefix="sl_sim_test_") as tmp:
        game = Game(data_dir=tmp, fresh=True)
        game.simulate_full_season(persist_each_slot=False)
        comps = {}
        for m in game.s["matches"]: comps[m["comp"]] = comps.get(m["comp"],0)+1
        assert len(game.s["matches"]) == 246, f"Cần 246 trận, hiện có {len(game.s['matches'])}"
        assert all(23 <= len(ids) <= 30 for ids in game.s["rosters"].values()), \
            "Mọi CLB phải duy trì đội hình trong khoảng 23–30 cầu thủ"
        assert all(len(game.league_table(t))==8 for t in ("premier","fc1","fc2"))
        assert comps.get("Premier League")==80 and comps.get("Football Championship 1")==58 and comps.get("Football Championship 2")==58
        assert comps.get("Cup")==23 and comps.get("C1 Play-in")==10 and comps.get("C1")==17
        assert len(game.s["history"])==1
        season_record = game.s["history"][-1]
        team_awards = season_record.get("team_awards") or []
        assert int(season_record.get("team_awards_version", 0) or 0) >= 1, \
            "Season archive phải có phiên bản dữ liệu danh hiệu tập thể"
        assert team_awards, "Mùa hoàn tất phải ghi nhận thành tích tập thể theo người chơi"
        assert all(int(row.get("appearances", 0) or 0) > 0 and row.get("club") for row in team_awards), \
            "Mọi danh hiệu tập thể phải có bằng chứng ra sân và CLB cụ thể"
        expected_team_competitions = {"Premier League", "Football Championship 1", \
                                      "Football Championship 2", "Major Cup", "Champions League"}
        assert expected_team_competitions.issubset({row.get("competition") for row in team_awards}), \
            "Thiếu dữ liệu danh hiệu tập thể ở một trong năm giải chính thức"
        assert len(game.s["slots"]) == 66
        assert game.s["calendar_validation"]["valid"] is True
        final_slot = max(game.s["slots"], key=lambda slot: slot["date"])
        assert final_slot["kind"] == "c1_final", "C1 Final phải là slot cuối cùng của mùa"
        premier_p2_r3 = next(slot for slot in game.s["slots"] if slot.get("id") == "P2R03")
        cup_final_slot = next(slot for slot in game.s["slots"] if slot.get("kind") == "cup_final")
        assert (datetime.fromisoformat(cup_final_slot["date"]) - datetime.fromisoformat(premier_p2_r3["date"])).days == 3
        _assert_match_schedule(game.s["matches"], int(game.config["calendar"].get("min_rest_days", 3)))
        assert max(game.s["matches"], key=lambda match: (match["date"], match["id"]))["stage"] == "c1_final"
        print("Smoke test OK: 246 trận, 66 slot; C1 Play-in xen kẽ lượt đi; Major Cup Final sau Premier GĐ2 vòng 3; C1 Final là trận cuối mùa; reset tạo career trắng, không backup.")
        print(f"  VĐ Premier: {game.s['history'][-1]['champion_premier']}")
        print(f"  VĐ Cup: {game.s['history'][-1]['champion_cup']}")
        print(f"  VĐ C1: {game.s['history'][-1]['champion_c1']}")
        print(f"  Danh hiệu tập thể: {len(team_awards)} bản ghi · "
              f"{len({row['player_id'] for row in team_awards})} cầu thủ · "
              f"{len(expected_team_competitions)} giải đấu")
    calibration = run_engine_calibration(2000, quiet=False)
    if not calibration["passed"]:
        raise AssertionError("Engine không đạt tiêu chí hiệu chỉnh M0; xem kết quả benchmark phía trên.")


def _assert_match_schedule(matches, min_rest_days):
    """Kiểm tra sau mô phỏng: mỗi CLB cách hai ngày thi đấu liên tiếp >= ngưỡng."""
    from datetime import date
    last = {}
    for match in sorted(matches, key=lambda item: (item["date"], item["id"])):
        current = date.fromisoformat(match["date"])
        for team in (match["home"], match["away"]):
            if team in last:
                gap = (current - last[team]).days
                if gap < min_rest_days:
                    raise AssertionError(f"{team}: lịch xung đột cách {gap} ngày ({last[team]} -> {current}).")
            last[team] = current


def main():
    parser = argparse.ArgumentParser(description="Super League Football Simulator")
    parser.add_argument("--fresh", action="store_true", help="Xóa sự nghiệp hiện tại và tạo lại trong SQLite duy nhất")
    parser.add_argument("--sim", type=int, default=0, help="Mô phỏng N mùa không mở giao diện")
    parser.add_argument("--seed", type=int, default=None, help="Seed để kết quả có thể tái lập")
    parser.add_argument("--check", action="store_true", help="Kiểm tra dữ liệu và chạy smoke test một mùa")
    parser.add_argument("--export", action="store_true", help="Xuất bảng xếp hạng/lịch thành CSV rồi thoát")
    parser.add_argument("--db-status", action="store_true", help="Kiểm tra SQLite và số lượng dữ liệu đang lưu")
    parser.add_argument("--sheets-status", action="store_true", help="Hiển thị trạng thái đồng bộ Google Sheets")
    parser.add_argument("--sheets-sync", action="store_true", help="Đồng bộ snapshot mùa hiện tại lên Google Sheets rồi thoát")
    parser.add_argument("--sheets-wait-seconds", type=float, default=60.0,
                        help="Thời gian chờ tối đa cho --sheets-sync (mặc định 60 giây)")
    parser.add_argument("--calibrate", nargs="?", const=2000, type=int, default=0,
                        help="Benchmark engine với N trận; bỏ N sẽ chạy 2000 trận mỗi mẫu")
    args = parser.parse_args()

    if args.db_status:
        game = Game(fresh=False, seed=args.seed, enable_google_sheets=False)
        try:
            print(json.dumps(game.database.status(), ensure_ascii=False, indent=2))
        finally:
            game.close_google_sheets_sync(timeout=1.0)
        return
    if args.check:
        run_checks(); return
    if args.calibrate:
        result = run_engine_calibration(args.calibrate, seed=args.seed)
        if not result["passed"]:
            raise SystemExit(1)
        return
    if args.sheets_status or args.sheets_sync:
        # Avoid an implicit startup sync when the user only asks for status.
        game = Game(fresh=False, seed=args.seed, enable_google_sheets=False)
        if args.sheets_sync:
            status = game.sync_to_google_sheets(wait=True, timeout=args.sheets_wait_seconds)
        else:
            status = game.google_sheets_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if args.sheets_sync and status.get("status") != "synced":
            raise SystemExit(2)
        return
    game = Game(fresh=args.fresh, seed=args.seed)
    if args.sim:
        n=max(1,args.sim)
        try:
            for i in range(n):
                if game.s["season_done"]: game.next_season()
                record=game.simulate_full_season(persist_each_slot=False)
                print(f"Mùa {record['label']} | Premier: {record['champion_premier']} | Cup: {record['champion_cup']} | C1: {record['champion_c1']} | {record['match_count']} trận")
                if i < n-1: game.next_season()
        finally:
            game.close_google_sheets_sync(timeout=10.0)
        return
    if args.export:
        try:
            print(game.export_csvs())
        finally:
            game.close_google_sheets_sync(timeout=3.0)
        return
    try:
        from sl_gui import SuperLeagueApp
        app = SuperLeagueApp(game)
        app.run()
    except Exception as exc:
        diagnostic = traceback.format_exc()
        print(f"Không mở được giao diện: {exc}", file=sys.stderr)
        print(diagnostic, file=sys.stderr)
        try:
            import tempfile
            log_path = Path(tempfile.gettempdir()) / "SuperLeague_gui_startup.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] GUI startup failed\n")
                log.write(f"Python: {sys.version}\nWorking directory: {Path.cwd()}\n")
                log.write(diagnostic + "\n")
            print(f"Chi tiết đã ghi vào: {log_path}", file=sys.stderr)
        except OSError:
            pass
        print("Bạn vẫn có thể kiểm tra Tkinter bằng: python3 test/gui_diagnostic.py", file=sys.stderr)
        print("Bạn vẫn có thể chạy CLI bằng: python3 src/main.py --sim 1 --fresh", file=sys.stderr)
        raise

if __name__ == "__main__": main()
