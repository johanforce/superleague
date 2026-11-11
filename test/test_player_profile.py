import unittest

from economy.player_profile import build_player_profile


class PlayerProfileAggregationTests(unittest.TestCase):
    def _state(self):
        return {
            "season": 2027,
            "players": {
                "P1": {"common_name": "Player One", "position": "CM", "overall_rating": 82,
                       "pace": 78.6, "passing": 86.4, "shooting": 71.1, "dribbling": 80.2,
                       "defending": 68.8, "physicality": 75.2},
            },
            "player_meta": {"P1": {"age": 20, "potential": 90}},
            "rosters": {"Club A": ["P1"]},
            "season_stats": {"P1": {"apps": 2, "starts": 2, "mins": 180, "goals": 1,
                                     "assists": 1, "rating_sum": 15.7, "rating_count": 2}},
            "player_career": {"P1": [{"season": 2026, "club": "Club A", "apps": 20,
                                      "minutes": 1800, "goals": 5, "assists": 4,
                                      "avg_rating": 7.45}]},
            "history": [{
                "season": 2026,
                "career_player_stats": [{"player_id": "P1", "player": "Player One", "team": "Club A",
                                         "position": "CM", "apps": 20, "minutes": 1800, "goals": 5,
                                         "assists": 4, "avg_rating": 7.45}],
                "development": [{"player_id": "P1", "new_ovr": 80, "old_ovr": 78,
                                 "delta": 2, "potential": 89, "team": "Club A"}],
                "season_awards": {"young_player": {"player_id": "P1", "team": "Club A"}},
                "competition_awards": {},
            }],
            "last_development": [{"player_id": "P1", "new_ovr": 82, "old_ovr": 80,
                                  "delta": 2, "potential": 90, "team": "Club A"}],
            "matches": [
                {"played": True, "comp": "Premier League", "home": "Club A", "away": "Club B",
                 "minutes_h": {"P1": 90}, "player_stats": {"h": {"P1": {"g": 1, "a": 0, "sh": 3,
                    "sot": 2, "xg": 0.76, "kp": 2, "tk": 1, "int": 1, "sv": 0, "rt": 8.1}}}},
                {"played": True, "comp": "Premier League", "home": "Club C", "away": "Club A",
                 "minutes_a": {"P1": 90}, "player_stats": {"a": {"P1": {"g": 0, "a": 1, "sh": 1,
                    "sot": 1, "xg": 0.31, "kp": 3, "tk": 2, "int": 1, "sv": 0, "rt": 7.6}}}},
            ],
        }

    def test_career_and_development_show_one_row_per_season(self):
        profile = build_player_profile(self._state(), "P1", season_label="2027/28")
        self.assertEqual([row["season"] for row in profile["career"]], [2026, 2027])
        self.assertEqual(profile["career"][0]["apps"], 20)  # archive wins over duplicate career cache
        self.assertEqual([row["season"] for row in profile["development"]["timeline"]], [2026, 2027])
        self.assertEqual(profile["development"]["timeline"][-1]["ovr"], 82)

    def test_single_season_row_preserves_club_count_after_midseason_transfer(self):
        state = self._state()
        state["player_career"]["P1"].append({"season": 2026, "club": "Club B",
                                                "apps": 4, "minutes": 360, "goals": 1})
        profile = build_player_profile(state, "P1", season_label="2027/28")
        season_2026 = [row for row in profile["career"] if row["season"] == 2026]
        self.assertEqual(len(season_2026), 1)
        self.assertEqual(profile["career_clubs"], ["Club A", "Club B"])
        # Do not double-count the cache when an archive total exists.
        self.assertEqual(profile["career_totals"]["apps"], 22)

    def test_competition_stats_are_aggregated_by_season_club_and_competition(self):
        profile = build_player_profile(self._state(), "P1", season_label="2027/28")
        self.assertEqual(len(profile["career_competitions"]), 1)
        premier = profile["career_competitions"][0]
        self.assertEqual(premier["club"], "Club A")
        self.assertEqual(premier["apps"], 2)
        self.assertEqual(premier["minutes"], 180)
        self.assertEqual(premier["goals"], 1)
        self.assertEqual(premier["assists"], 1)

    def test_legacy_young_player_is_counted_as_golden_boy(self):
        profile = build_player_profile(self._state(), "P1", season_label="2027/28")
        golden_boy_rows = [row for row in profile["development"]["awards"]
                           if row.get("award_id") == "golden_boy"]
        self.assertEqual(len(golden_boy_rows), 1)
        self.assertEqual(golden_boy_rows[0]["title"], "Golden Boy")

    def test_canonical_individual_award_ledger_is_used(self):
        state = self._state()
        state["history"][0]["individual_awards_version"] = 1
        state["history"][0]["individual_awards"] = [{
            "player_id": "P1", "award_id": "golden_boy", "title": "Golden Boy",
            "competition": "Toàn mùa", "club": "Club A",
        }]
        profile = build_player_profile(state, "P1", season_label="2027/28")
        self.assertEqual(sum(row.get("award_id") == "golden_boy"
                             for row in profile["development"]["awards"]), 1)

    def test_player_attributes_are_normalized_to_integer_values(self):
        profile = build_player_profile(self._state(), "P1", season_label="2027/28")
        self.assertTrue(all(isinstance(value, int) for _label, value in profile["attributes"]))
        self.assertTrue(all(isinstance(value, int) for _label, value in profile["axes"]))
        self.assertEqual(profile["season"]["apps"], 2)
        self.assertAlmostEqual(profile["season"]["avg_rating"], 7.85)


if __name__ == "__main__":
    unittest.main(verbosity=2)
