package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/yunsur/douyin-mcp/douyin"
)

type scanRequest struct {
	SecUID string `json:"sec_uid"`
	Cookie string `json:"cookie"`
	MaxItems int `json:"max_items"`
}
type scanResponse struct {
	Status string `json:"status"`
	Videos []any `json:"videos,omitempty"`
	VideoCount int `json:"video_count"`
	Error string `json:"error,omitempty"`
}

type argusHeaderTransport struct { base douyin.Transport }

// Douyin's web gateway requires this header before it evaluates the UIFID
// and web-signature fields. Inject it consistently on every Douyin request.
func (t argusHeaderTransport) Do(ctx context.Context, method, rawURL string, headers douyin.Headers, cookieHeader string, body []byte) (*douyin.Response, error) {
	if strings.Contains(strings.ToLower(rawURL), "douyin.com") {
		headers.Set("x-tt-argus", "1")
	}
	return t.base.Do(ctx, method, rawURL, headers, cookieHeader, body)
}
func (t argusHeaderTransport) Get(ctx context.Context, rawURL string, headers douyin.Headers, cookieHeader string) (*douyin.Response, error) {
	return t.Do(ctx, http.MethodGet, rawURL, headers, cookieHeader, nil)
}
func (t argusHeaderTransport) PostJSON(ctx context.Context, rawURL string, headers douyin.Headers, cookieHeader, contentType string, body []byte) (*douyin.Response, error) {
	if contentType != "" { headers.Set("content-type", contentType) }
	return t.Do(ctx, http.MethodPost, rawURL, headers, cookieHeader, body)
}

// The web session may expose UIFID_TEMP before UIFID. The Go client signs
// requests from the canonical UIFID cookie name, so promote the temporary
// platform-issued value without fabricating a new device identifier.
func normalizeDouyinCookie(raw string) string {
	parts := strings.Split(raw, ";")
	out := make([]string, 0, len(parts)+1)
	tempUIFID := ""
	hasUIFID := false
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" { continue }
		name, value, found := strings.Cut(part, "=")
		name = strings.TrimSpace(name)
		if found && strings.EqualFold(name, "UIFID") {
			hasUIFID = true
			out = append(out, "UIFID="+strings.TrimSpace(value))
			continue
		}
		if found && strings.EqualFold(name, "UIFID_TEMP") {
			tempUIFID = strings.TrimSpace(value)
			out = append(out, "UIFID_TEMP="+tempUIFID)
			continue
		}
		out = append(out, part)
	}
	if !hasUIFID && tempUIFID != "" { out = append(out, "UIFID="+tempUIFID) }
	return strings.Join(out, "; ")
}
var secUIDPattern = regexp.MustCompile(`^MS4wLjAB[A-Za-z0-9_-]{10,240}$`)

func scan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	r.Body = http.MaxBytesReader(w, r.Body, 20000)
	var req scanRequest
	decoder := json.NewDecoder(r.Body)
	decoder.UseNumber() // Preserve large Douyin pagination cursors without float64 rounding.
	if err := decoder.Decode(&req); err != nil { http.Error(w, "invalid JSON", http.StatusBadRequest); return }
	req.SecUID = strings.TrimSpace(req.SecUID)
	req.Cookie = normalizeDouyinCookie(strings.TrimSpace(req.Cookie))
	if !secUIDPattern.MatchString(req.SecUID) || req.Cookie == "" { http.Error(w, "sec_uid or cookie invalid", http.StatusBadRequest); return }
	if req.MaxItems < 1 { req.MaxItems = 600 }
	if req.MaxItems > 600 { req.MaxItems = 600 }
	ctx, cancel := context.WithTimeout(r.Context(), 7*time.Minute)
	defer cancel()
	baseTransport, err := douyin.NewHTTPClient(douyin.ProxyFromEnv())
	if err != nil { write(w, http.StatusBadGateway, scanResponse{Status:"error", Error:"cannot initialize Douyin HTTP transport"}); return }
	transport := argusHeaderTransport{base: baseTransport}
	client, err := douyin.NewClient(req.Cookie, douyin.Options{Transport: transport})
	if err != nil { write(w, http.StatusBadGateway, scanResponse{Status:"error", Error:"cannot initialize Douyin session"}); return }
	posts, err := scanAllPosts(ctx, client, "https://www.douyin.com/user/"+req.SecUID, req.MaxItems)
	if err != nil {
		log.Printf("Douyin scan failed sec_uid=%s err=%v", req.SecUID[:18], err)
		write(w, http.StatusBadGateway, scanResponse{Status:"error", Error: safeError(err)})
		return
	}
	if posts == nil { posts = []any{} }
	write(w, http.StatusOK, scanResponse{Status:"success", Videos:posts, VideoCount:len(posts)})
}
type douyinWorksClient interface {
	GetUserWorkInfo(context.Context, string, string) (map[string]any, error)
}

func scanAllPosts(ctx context.Context, client douyinWorksClient, profileURL string, limit int) ([]any, error) {
	const pageSize = 18
	maxPages := (limit + pageSize - 1) / pageSize + 2
	cursor := "0"
	seen := make(map[string]bool)
	posts := make([]any, 0, limit)
	for page := 0; page < maxPages && len(posts) < limit; page++ {
		result, err := client.GetUserWorkInfo(ctx, profileURL, cursor)
		if err != nil { return nil, err }
		raw, ok := result["aweme_list"].([]any)
		if !ok {
			if status, exists := result["status_msg"]; exists {
				return nil, fmt.Errorf("Douyin feed rejected request: %v", status)
			}
			return nil, fmt.Errorf("Douyin feed response has no aweme_list")
		}
		before := len(posts)
		for _, item := range raw {
			record, ok := item.(map[string]any)
			if !ok { continue }
			id := fmt.Sprint(record["aweme_id"])
			if id == "" || id == "<nil>" { id = fmt.Sprint(record["id"]) }
			if id == "" || id == "<nil>" || seen[id] { continue }
			seen[id] = true
			posts = append(posts, record)
			if len(posts) >= limit { break }
		}
		hasMore := fmt.Sprint(result["has_more"]) == "1" || fmt.Sprint(result["has_more"]) == "true"
		nextCursor := fmt.Sprint(result["max_cursor"])
		if !hasMore || len(posts) == before || nextCursor == "" || nextCursor == "<nil>" || nextCursor == cursor {
			break
		}
		cursor = nextCursor
	}
	if len(posts) > limit { posts = posts[:limit] }
	return posts, nil
}

func safeError(err error) string {
	if errors.Is(err, context.DeadlineExceeded) { return "Douyin request timed out" }
	msg := err.Error()
	if len(msg)>240 { msg=msg[:240] }
	return msg
}
func write(w http.ResponseWriter, status int, v scanResponse) {
	w.Header().Set("Content-Type","application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func main() {
	http.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK); _,_=w.Write([]byte("ok")) })
	http.HandleFunc("/scan", scan)
	log.Fatal(http.ListenAndServe("127.0.0.1:18081", nil))
}
