// bjca: exact blackjack combinatorial analyzer (command line front end).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <vector>

#include "analyze.hpp"
#include "split_td.hpp"

using namespace bj;

static const char *RANK_NAMES = "A23456789T";

static int parse_card(const std::string &s) {
  if (s == "A" || s == "a" || s == "1" || s == "11") return ACE;
  if (s == "T" || s == "t" || s == "10" || s == "J" || s == "j" || s == "Q" || s == "q" || s == "K" || s == "k")
    return TEN;
  if (s.size() == 1 && s[0] >= '2' && s[0] <= '9') return s[0] - '1';
  fprintf(stderr, "bad card '%s'\n", s.c_str());
  exit(2);
}
static std::vector<std::string> split_list(const std::string &s) {
  std::vector<std::string> out;
  std::string cur;
  for (char ch : s) {
    if (ch == ',' || ch == ' ') {
      if (!cur.empty()) out.push_back(cur), cur.clear();
    } else
      cur += ch;
  }
  if (!cur.empty()) out.push_back(cur);
  return out;
}
static Counts parse_cards(const std::string &s) {
  Counts c{};
  for (auto &t : split_list(s)) c[parse_card(t)]++;
  return c;
}
static std::string num(double v) {
  if (v == NEG_INF) return "null";
  char buf[64];
  snprintf(buf, sizeof buf, "%.15g", v);
  return buf;
}
static std::string numh(double v) {
  if (v == NEG_INF) return "      --     ";
  char buf[64];
  snprintf(buf, sizeof buf, "%+.10f", v);
  return buf;
}

static void usage() {
  fprintf(stderr,
          "usage:\n"
          "  bjca round [shoe] [rules] [--table] [--json]   exact EV of the next round + full strategy\n"
          "  bjca hand --player C,C[,C...] --up C [shoe] [rules] [--json]\n"
          "                                                 exact EV of every action for one situation\n"
          "       add --split-pair C --pending M --splits-left S for a hand that came from a split\n"
          "       (M hands still waiting for a second card; list every other card seen in --seen)\n"
          "  bjca bench [shoe] [rules]                      timings\n"
          "  bjca bench-case --case ID [--variant V]        one case of the fixed benchmark suite (JSON)\n"
          "shoe:\n"
          "  --decks N            start from N full decks (default 6)\n"
          "  --seen C,C,...       cards already dealt/discarded (removed from the decks)\n"
          "  --unseen a,2,3,...,t ten counts (A,2..9,T) of exactly the cards not visible to the player;\n"
          "                       with `hand`, the player's cards and upcard are NOT removed again\n"
          "rules (defaults: S17, DAS, double any two, 3 resplits, one card to split aces, no surrender, 3:2):\n"
          "  --h17  --no-das  --double-on any|9-11|10-11  --max-splits N\n"
          "  --resplit-aces  --hit-split-aces  --surrender none|early|late  --bj-payout X\n"
          "other:\n"
          "  --threads N\n");
  exit(2);
}

struct Args {
  std::string cmd;
  Rules rules;
  int decks = 6;
  Counts seen{};
  bool have_unseen = false;
  Counts unseen{};
  std::string player, up, split_pair, bench_case, variant = "current";
  int pending = 0, splits_left = 0;
  bool json = false, table = false;
  int threads = 0;
};

static Args parse(int argc, char **argv) {
  Args a;
  a.rules.resplit_aces = false;
  a.rules.hit_split_aces = false;
  if (argc < 2) usage();
  a.cmd = argv[1];
  for (int i = 2; i < argc; ++i) {
    std::string f = argv[i];
    auto val = [&]() -> std::string {
      if (i + 1 >= argc) usage();
      return argv[++i];
    };
    if (f == "--decks") a.decks = atoi(val().c_str());
    else if (f == "--seen") { Counts c = parse_cards(val()); for (int r = 0; r < NR; ++r) a.seen[r] += c[r]; }
    else if (f == "--unseen") {
      auto v = split_list(val());
      if (v.size() != NR) usage();
      for (int r = 0; r < NR; ++r) a.unseen[r] = atoi(v[r].c_str());
      a.have_unseen = true;
    }
    else if (f == "--player") a.player = val();
    else if (f == "--up") a.up = val();
    else if (f == "--split-pair") a.split_pair = val();
    else if (f == "--pending") a.pending = atoi(val().c_str());
    else if (f == "--splits-left") a.splits_left = atoi(val().c_str());
    else if (f == "--h17") a.rules.h17 = true;
    else if (f == "--no-das") a.rules.das = false;
    else if (f == "--double-on") {
      std::string v = val();
      a.rules.double_on = v == "any" ? DoubleOn::Any : v == "9-11" ? DoubleOn::NineToEleven : DoubleOn::TenToEleven;
    }
    else if (f == "--max-splits") a.rules.max_splits = atoi(val().c_str());
    else if (f == "--resplit-aces") a.rules.resplit_aces = true;
    else if (f == "--hit-split-aces") a.rules.hit_split_aces = true;
    else if (f == "--surrender") {
      std::string v = val();
      a.rules.surrender = v == "early" ? Surrender::Early : v == "late" ? Surrender::Late : Surrender::None;
    }
    else if (f == "--bj-payout") a.rules.bj_payout = atof(val().c_str());
    else if (f == "--threads") a.threads = atoi(val().c_str());
    else if (f == "--case") a.bench_case = val();
    else if (f == "--variant") a.variant = val();
    else if (f == "--json") a.json = true;
    else if (f == "--table") a.table = true;
    else usage();
  }
  return a;
}

static std::string rules_str(const Rules &r) {
  std::ostringstream o;
  o << (r.h17 ? "H17" : "S17") << (r.das ? " DAS" : " noDAS");
  o << (r.double_on == DoubleOn::Any ? " D-any" : r.double_on == DoubleOn::NineToEleven ? " D9-11" : " D10-11");
  o << " SPL" << r.max_splits << (r.resplit_aces ? " RSA" : " noRSA") << (r.hit_split_aces ? " HSA" : " noHSA");
  o << (r.surrender == Surrender::None ? "" : r.surrender == Surrender::Early ? " ES" : " LS");
  o << " BJ" << r.bj_payout;
  return o.str();
}

static std::string hand_json(const HandResult &h) {
  std::ostringstream o;
  o << "{\"stand\":" << num(h.stand) << ",\"hit\":" << num(h.hit) << ",\"double\":" << num(h.dbl)
    << ",\"split\":" << num(h.split) << ",\"surrender\":" << num(h.surrender)
    << ",\"best\":\"" << action_name(h.best) << "\",\"ev_play\":" << num(h.ev_play)
    << ",\"ev\":" << num(h.ev) << ",\"p_dealer_natural\":" << num(h.p_dealer_natural)
    << ",\"natural\":" << (h.natural ? "true" : "false") << ",\"prob\":" << num(h.prob) << "}";
  return o.str();
}

static int cmd_round(const Args &a) {
  Counts shoe;
  if (a.have_unseen) shoe = a.unseen;
  else {
    shoe = full_shoe(a.decks);
    for (int r = 0; r < NR; ++r) shoe[r] -= a.seen[r];
  }
  for (int r = 0; r < NR; ++r)
    if (shoe[r] < 0) { fprintf(stderr, "negative card count\n"); return 2; }
  Engine eng(a.rules, a.threads);
  auto res = eng.analyze_round(shoe);
  if (a.json) {
    printf("{\"ev\":%s,\"seconds\":%.6f,\"cells\":[", num(res.ev).c_str(), res.seconds);
    bool first = true;
    for (int u = 0; u < NR; ++u)
      for (int c1 = 0; c1 < NR; ++c1)
        for (int c2 = c1; c2 < NR; ++c2) {
          const auto &h = res.cell[u][c1][c2];
          if (h.prob <= 0) continue;
          printf("%s{\"up\":\"%c\",\"hand\":\"%c%c\",\"r\":%s}", first ? "" : ",", RANK_NAMES[u],
                 RANK_NAMES[c1], RANK_NAMES[c2], hand_json(h).c_str());
          first = false;
        }
    printf("]}\n");
    return 0;
  }
  printf("rules: %s\n", rules_str(a.rules).c_str());
  printf("shoe (A,2..9,T):");
  for (int r = 0; r < NR; ++r) printf(" %d", shoe[r]);
  printf("  [%d cards]\n", total_cards(shoe));
  printf("exact EV of the round with perfect play: %+.12f  (%+.6f%%)\n", res.ev, 100 * res.ev);
  printf("time: %.3fs total, %.3fs in splits (%zu split nodes, %d threads)\n", res.seconds,
         res.split_seconds, res.split_nodes, eng.threads);
  if (a.table) {
    printf("\nbest action by starting hand (rows) and dealer upcard (columns)\n");
    printf("S stand, H hit, D double, P split, R surrender\n\n      ");
    for (int u = 1; u < NR; ++u) printf(" %c", RANK_NAMES[u]);
    printf(" A\n");
    static const char L[] = "SHDPR";
    for (int c1 = 0; c1 < NR; ++c1)
      for (int c2 = c1; c2 < NR; ++c2) {
        printf("  %c,%c ", RANK_NAMES[c1], RANK_NAMES[c2]);
        for (int k = 1; k <= NR; ++k) {
          int u = k % NR;
          const auto &h = res.cell[u][c1][c2];
          printf(" %c", h.prob > 0 ? (h.natural ? '*' : L[h.best]) : '.');
        }
        printf("\n");
      }
  }
  return 0;
}

static int cmd_hand(const Args &a) {
  if (a.player.empty() || a.up.empty()) usage();
  Counts hand = parse_cards(a.player);
  int up = parse_card(a.up);
  Counts unseen;
  if (a.have_unseen) unseen = a.unseen;
  else {
    unseen = full_shoe(a.decks);
    for (int r = 0; r < NR; ++r) unseen[r] -= a.seen[r] + hand[r];
    unseen[up]--;
  }
  for (int r = 0; r < NR; ++r)
    if (unseen[r] < 0) { fprintf(stderr, "negative card count\n"); return 2; }
  Engine eng(a.rules, a.threads);
  auto t0 = std::chrono::steady_clock::now();
  HandResult h = a.split_pair.empty()
                     ? eng.analyze_hand(unseen, hand, up)
                     : eng.analyze_split_hand(unseen, parse_card(a.split_pair), hand, up, a.pending, a.splits_left);
  double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  if (a.json) {
    printf("%s\n", hand_json(h).c_str());
    return 0;
  }
  printf("rules: %s\n", rules_str(a.rules).c_str());
  printf("player %s vs dealer %s, unseen (A,2..9,T):", a.player.c_str(), a.up.c_str());
  for (int r = 0; r < NR; ++r) printf(" %d", unseen[r]);
  printf("  [%d cards]\n", total_cards(unseen));
  if (h.natural) {
    printf("blackjack. EV %+.10f (dealer natural probability %.10f)\n", h.ev, h.p_dealer_natural);
    return 0;
  }
  if (!a.split_pair.empty())
    printf("split hand (%d more waiting, %d splits left): values are this hand plus all unplayed hands\n",
           a.pending, a.splits_left);
  printf("exact EV per unit bet, given the dealer has no natural:\n");
  printf("  stand     %s\n", numh(h.stand).c_str());
  printf("  hit       %s\n", numh(h.hit).c_str());
  printf("  double    %s\n", numh(h.dbl).c_str());
  printf("  split     %s\n", numh(h.split).c_str());
  printf("  surrender %s\n", numh(h.surrender).c_str());
  printf("best: %s   (EV %+.10f; %+.10f before the dealer checks for a natural, p=%.6f)\n",
         action_name(h.best), h.ev_play, h.ev, h.p_dealer_natural);
  printf("time: %.3fs", sec);
  if (h.split_nodes) printf("  (%zu split nodes)", h.split_nodes);
  printf("\n");
  return 0;
}

static int cmd_bench(const Args &a) {
  Counts shoe = full_shoe(a.decks);
  for (int r = 0; r < NR; ++r) shoe[r] -= a.seen[r];
  printf("rules: %s, %d decks\n", rules_str(a.rules).c_str(), a.decks);
  for (int ms = 0; ms <= a.rules.max_splits; ++ms) {
    Rules r = a.rules;
    r.max_splits = ms;
    Engine eng(r, a.threads);
    auto res = eng.analyze_round(shoe);
    printf("max_splits=%d: EV %+.12f  %.3fs (%zu split nodes)\n", ms, res.ev, res.seconds, res.split_nodes);
    fflush(stdout);
  }
  return 0;
}

// One case of the fixed benchmark suite, solved by one of the solver
// iterations kept in the tree. Prints a single JSON object.
//   variants: carry-all     hash-memoised recursion carrying every finished hand
//             settle-early  + finished hands settled immediately (martingale)
//             layered       + layer-by-layer streaming solver, one thread
//             bfs-dealer    + breadth-first dealer evaluation, one thread
//             current       + all threads (whatever the tree does today)
static int cmd_bench_case(const Args &a) {
  Rules rules;  // S17, DAS, double any two, one card to split aces, no surrender
  rules.resplit_aces = false;
  rules.hit_split_aces = false;
  SplitAlgo algo = SplitAlgo::Layered;
  int threads = a.threads;
  g_dealer_depth_first = false;
  if (a.variant == "carry-all") algo = SplitAlgo::CarryAll, threads = 1, g_dealer_depth_first = true;
  else if (a.variant == "settle-early") algo = SplitAlgo::TopDown, threads = 1, g_dealer_depth_first = true;
  else if (a.variant == "layered") threads = 1, g_dealer_depth_first = true;
  else if (a.variant == "bfs-dealer") threads = 1;
  else if (a.variant != "current") { fprintf(stderr, "unknown variant\n"); return 2; }

  struct Case { const char *id; int decks, max_splits; const char *player, *up; };
  static const Case cases[] = {
      {"hand-88vT-6d", 6, 3, "8,8", "T"},
      {"hand-22v6-8d", 8, 3, "2,2", "6"},
      {"round-6d-spl1", 6, 1, nullptr, nullptr},
      {"round-6d-spl3", 6, 3, nullptr, nullptr},
  };
  for (const Case &cs : cases) {
    if (a.bench_case != cs.id) continue;
    rules.max_splits = cs.max_splits;
    Engine eng(rules, threads);
    eng.algo = algo;
    eng.state_limit = 60u * 1000 * 1000;  // ~5 GB of hash table
    double ev = 0;
    size_t nodes = 0;
    auto t0 = std::chrono::steady_clock::now();
    try {
      if (cs.player) {
        Counts hand = parse_cards(cs.player);
        int up = parse_card(cs.up);
        Counts unseen = full_shoe(cs.decks);
        for (int r = 0; r < NR; ++r) unseen[r] -= hand[r];
        unseen[up]--;
        HandResult h = eng.analyze_hand(unseen, hand, up);
        ev = h.split;
        nodes = h.split_nodes;
      } else {
        auto res = eng.analyze_round(full_shoe(cs.decks));
        ev = res.ev;
        nodes = res.split_nodes;
      }
    } catch (const std::exception &e) {
      double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
      printf("{\"case\":\"%s\",\"variant\":\"%s\",\"error\":\"%s\",\"seconds\":%.6f}\n", cs.id,
             a.variant.c_str(), e.what(), sec);
      return 3;
    }
    double sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    printf("{\"case\":\"%s\",\"variant\":\"%s\",\"seconds\":%.6f,\"ev\":%.15g,\"nodes\":%zu,\"threads\":%d}\n",
           cs.id, a.variant.c_str(), sec, ev, nodes, eng.threads);
    return 0;
  }
  fprintf(stderr, "unknown case; cases:");
  for (const Case &cs : cases) fprintf(stderr, " %s", cs.id);
  fprintf(stderr, "\n");
  return 2;
}

int main(int argc, char **argv) {
  Args a = parse(argc, argv);
  try {
    if (a.cmd == "round") return cmd_round(a);
    if (a.cmd == "hand") return cmd_hand(a);
    if (a.cmd == "bench") return cmd_bench(a);
    if (a.cmd == "bench-case") return cmd_bench_case(a);
  } catch (const std::exception &e) {
    fprintf(stderr, "error: %s\n", e.what());
    return 1;
  }
  usage();
}
