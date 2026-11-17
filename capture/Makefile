# Builds dlcap against the Blackmagic DeckLink SDK headers.
#   make DECKLINK_SDK_INCLUDE=/path/to/Blackmagic\ DeckLink\ SDK/Linux/include

DECKLINK_SDK_INCLUDE ?= /opt/decklink-sdk/include
PREFIX ?= /usr/local

CXX ?= g++
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++17 -Wall -Wextra -Wno-unused-parameter -pthread
LDLIBS += -ldl -pthread

all: dlcap

dlcap: dlcap.o DeckLinkAPIDispatch.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDLIBS)

dlcap.o: src/dlcap.cpp
	@test -f "$(DECKLINK_SDK_INCLUDE)/DeckLinkAPI.h" || \
		{ echo "DeckLinkAPI.h not found in $(DECKLINK_SDK_INCLUDE); set DECKLINK_SDK_INCLUDE"; exit 1; }
	$(CXX) $(CXXFLAGS) -I"$(DECKLINK_SDK_INCLUDE)" -c -o $@ $<

# Blackmagic's loader for libDeckLinkAPI.so (dlopen'd at runtime).
DeckLinkAPIDispatch.o: $(DECKLINK_SDK_INCLUDE)/DeckLinkAPIDispatch.cpp
	$(CXX) $(CXXFLAGS) -w -I"$(DECKLINK_SDK_INCLUDE)" -c -o $@ "$<"

install: dlcap
	install -D -m 0755 dlcap $(DESTDIR)$(PREFIX)/bin/dlcap

clean:
	rm -f dlcap *.o

.PHONY: all install clean
