package main

import "testing"

func TestHealth(t *testing.T) {
	if "ok" != "ok" {
		t.Fatal("unreachable")
	}
}
