CC      = gcc
CFLAGS  = -O3 -Wall
LDFLAGS = -lm -pthread

all: idefs_crack idefs_crack_parallel idefs_crack_mitm

idefs_crack: src/idefs_crack.c
	$(CC) $(CFLAGS) -o $@ $<

idefs_crack_parallel: src/idefs_crack_parallel.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

idefs_crack_mitm: src/idefs_crack_mitm.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

test: all
	./idefs_crack --self-test
	./idefs_crack_parallel --self-test
	./idefs_crack_mitm --self-test

clean:
	rm -f idefs_crack idefs_crack_parallel idefs_crack_mitm

.PHONY: all test clean
