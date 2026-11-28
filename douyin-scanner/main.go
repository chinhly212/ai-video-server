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
var secUIDPattern = regexp.MustCompile(`^MS4wLjAB[A-Za-z0-9_-]{10,240}$`)

func scan(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost { http.Error(w, "method not allowed", http.StatusMethodNotAllowed); return }
	r.Body = http.MaxBytesReader(w, r.Body, 20000)
	var req scanRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil { http.Error(w, "invalid JSON", http.StatusBadRequest); return }
	req.SecUID = strings.TrimSpace(req.SecUID)
	req.Cookie = strings.TrimSpace(req.Cookie)
	if !secUIDPattern.MatchString(req.SecUID) || req.Cookie == "" { http.Error(w, "sec_uid or cookie invalid", http.StatusBadRequest); return }
	if req.MaxItems < 1 || req.MaxItems > 600 { req.MaxItems = 600 }
	ctx, cancel := context.WithTimeout(r.Context(), 7*time.Minute)
	defer cancel()
	client, err := douyin.NewClient(req.Cookie, douyin.Options{})
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
func scanAllPosts(ctx context.Context, client *douyin.Client, profileURL string, limit int) ([]any, error) {
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
