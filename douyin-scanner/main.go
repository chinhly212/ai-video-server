package main

import (
	"context"
	"encoding/json"
	"errors"
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
	posts, err := client.GetUserAllWorkInfo(ctx, "https://www.douyin.com/user/"+req.SecUID, req.MaxItems)
	if err != nil {
		log.Printf("Douyin scan failed sec_uid=%s err=%v", req.SecUID[:18], err)
		write(w, http.StatusBadGateway, scanResponse{Status:"error", Error: safeError(err)})
		return
	}
	if posts == nil { posts = []any{} }
	write(w, http.StatusOK, scanResponse{Status:"success", Videos:posts, VideoCount:len(posts)})
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
