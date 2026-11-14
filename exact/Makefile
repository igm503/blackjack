CXX ?= c++
CXXFLAGS ?= -std=c++17 -O3 -march=native -Wall -Wextra -pthread
HDRS = bj.hpp split_td.hpp split_layered.hpp split_ref.hpp analyze.hpp

all: bjca

bjca: bjca.cpp $(HDRS)
	$(CXX) $(CXXFLAGS) -o $@ bjca.cpp

test_exact: test_exact.cpp $(HDRS)
	$(CXX) $(CXXFLAGS) -o $@ test_exact.cpp

# quick: about 1 minute of C++ checks plus the 5-minute brute-force comparison
test: bjca test_exact
	./test_exact quick
	python3 test_reference.py

# the full C++ suite takes about 13 minutes
test-full: bjca test_exact
	./test_exact
	python3 test_reference.py

clean:
	rm -f bjca test_exact

.PHONY: all test test-full clean
