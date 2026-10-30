# badchan: one source, one object per MCS ceiling. Normally driven by buffer-control-experiments/badchan.sh:
#     badchan.sh build mcs10 fade100of400
# which runs:   make one NAME=<name> MCS=<N> [ON=<ms> PERIOD=<ms>] [TRACE=1]

PROTO_AND_SCHEMA :=

include ../Makefile.defs

NAME ?= mcs10
MCS  ?= 10
ON   ?= 0
PERIOD ?= 0
TRACE ?= 0
TRACEFLAG := $(if $(filter 1,$(TRACE)),-DUSE_TRACE,)

one:
	@echo "--------- badchan.cpp -> badchan_$(NAME).o (MCS_CAP=$(MCS) FADE_ON_MS=$(ON) FADE_PERIOD_MS=$(PERIOD)) ---------"
	@$(CXX) $(CXXFLAGS) -DMCS_CAP=$(MCS) -DFADE_ON_MS=$(ON) -DFADE_PERIOD_MS=$(PERIOD) $(TRACEFLAG) $(INC) -c badchan.cpp -o badchan_$(NAME).o
	@$(VERIFIER_BIN) badchan_$(NAME).o >/dev/null 2>&1 \
		&& echo "verifier: OK" \
		|| { echo "verifier: FAILED -- the gNB would reject badchan_$(NAME).o"; rm -f badchan_$(NAME).o; exit 1; }

clean:
	rm -f *.o

cleanall: clean

.PHONY: one clean cleanall
