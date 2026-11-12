CXX ?= c++
CXXFLAGS ?= -std=c++17 -O3 -march=native -Wall -Wextra -pthread
HDRS = bj.hpp split_td.hpp split_layered.hpp split_ref.hpp analyze.hpp

all: bjca

bjca: bjca.cpp $(HDRS)
	$(CXX) $(CXXFLAGS) -o $@ bjca.cpp

test_exact: test_exact.cpp $(HDRS)
	$(CXX) $(CXXFLAGS) -o $@ test_exact.cpp

test: bjca test_exact
	./test_exact
	python3 test_reference.py

clean:
	rm -f bjca test_exact proto

.PHONY: all test clean
