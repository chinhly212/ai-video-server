package main

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

type fakeWorksClient struct {
	pages map[string]map[string]any
	errAt string
	calls []string
}

func (f *fakeWorksClient) GetUserWorkInfo(_ context.Context, _ string, cursor string) (map[string]any, error) {
	f.calls = append(f.calls, cursor)
	if cursor == f.errAt && f.errAt != "" {
		return nil, errors.New("upstream unavailable")
	}
	page, ok := f.pages[cursor]
	if !ok {
		return map[string]any{"aweme_list": []any{}, "has_more": 0}, nil
	}
	return page, nil
}

func work(id string) any {
	return map[string]any{"aweme_id": id}
}

func TestScanAllPostsPaginatesDeduplicatesAndPreservesCursor(t *testing.T) {
	const next = "9007199254740993"
	client := &fakeWorksClient{pages: map[string]map[string]any{
		"0": {
			"aweme_list": []any{work("a"), work("b")},
			"has_more": 1,
			"max_cursor": next,
		},
		next: {
			"aweme_list": []any{work("b"), work("c"), work("d")},
			"has_more": 0,
			"max_cursor": "0",
		},
	}}

	got, err := scanAllPosts(context.Background(), client, "https://www.douyin.com/user/test", 3)
	if err != nil {
		t.Fatalf("scanAllPosts returned error: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("got %d posts, want 3", len(got))
	}
	if !reflect.DeepEqual(client.calls, []string{"0", next}) {
		t.Fatalf("cursor calls = %#v, want exact cursor %#v", client.calls, []string{"0", next})
	}
	ids := make([]string, 0, len(got))
	for _, item := range got {
		ids = append(ids, item.(map[string]any)["aweme_id"].(string))
	}
	if !reflect.DeepEqual(ids, []string{"a", "b", "c"}) {
		t.Fatalf("post IDs = %#v, want [a b c]", ids)
	}
}

func TestScanAllPostsHonorsSmallLimit(t *testing.T) {
	client := &fakeWorksClient{pages: map[string]map[string]any{
		"0": {
			"aweme_list": []any{work("one"), work("two")},
			"has_more": 1,
			"max_cursor": "next",
		},
	}}
	got, err := scanAllPosts(context.Background(), client, "https://www.douyin.com/user/test", 1)
	if err != nil {
		t.Fatalf("scanAllPosts returned error: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d posts, want 1", len(got))
	}
	if !reflect.DeepEqual(client.calls, []string{"0"}) {
		t.Fatalf("unexpected extra page requests: %#v", client.calls)
	}
}

func TestScanAllPostsStopsWhenPageHasNoProgress(t *testing.T) {
	client := &fakeWorksClient{pages: map[string]map[string]any{
		"0": {
			"aweme_list": []any{work("same")},
			"has_more": 1,
			"max_cursor": "next",
		},
		"next": {
			"aweme_list": []any{work("same")},
			"has_more": 1,
			"max_cursor": "later",
		},
	}}
	got, err := scanAllPosts(context.Background(), client, "https://www.douyin.com/user/test", 10)
	if err != nil {
		t.Fatalf("scanAllPosts returned error: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d posts, want one deduplicated post", len(got))
	}
	if !reflect.DeepEqual(client.calls, []string{"0", "next"}) {
		t.Fatalf("unexpected cursor calls: %#v", client.calls)
	}
}

func TestScanAllPostsReturnsUpstreamError(t *testing.T) {
	client := &fakeWorksClient{errAt: "0"}
	if _, err := scanAllPosts(context.Background(), client, "https://www.douyin.com/user/test", 10); err == nil {
		t.Fatal("expected upstream error")
	}
}

func TestNormalizeDouyinCookiePromotesTemporaryUIFID(t *testing.T) {
	got := normalizeDouyinCookie("sessionid=abc; UIFID_TEMP=temp-issued; ttwid=xyz")
	want := "sessionid=abc; UIFID_TEMP=temp-issued; ttwid=xyz; UIFID=temp-issued"
	if got != want { t.Fatalf("normalized cookie = %q, want %q", got, want) }
}

func TestNormalizeDouyinCookiePreservesRealUIFID(t *testing.T) {
	got := normalizeDouyinCookie("sessionid=abc; uIfId=real-issued; UIFID_TEMP=temp-issued")
	want := "sessionid=abc; UIFID=real-issued; UIFID_TEMP=temp-issued"
	if got != want { t.Fatalf("normalized cookie = %q, want %q", got, want) }
}

func TestNormalizeDouyinCookieDoesNotInventUIFID(t *testing.T) {
	got := normalizeDouyinCookie("sessionid=abc; ttwid=xyz")
	want := "sessionid=abc; ttwid=xyz"
	if got != want { t.Fatalf("normalized cookie = %q, want %q", got, want) }
}
