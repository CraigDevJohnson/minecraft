package main

import (
	"context"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// fakeS3 answers a listing of the backup bucket from pages of objects, as S3 pages a long listing, and records
// what it was asked for.
type fakeS3 struct {
	mu    sync.Mutex
	pages [][]s3types.Object
	err   error
	asked []s3.ListObjectsV2Input
}

func (f *fakeS3) ListObjectsV2(_ context.Context, input *s3.ListObjectsV2Input, _ ...func(*s3.Options)) (*s3.ListObjectsV2Output, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, *input)
	if f.err != nil {
		return nil, f.err
	}
	page := 0
	if input.ContinuationToken != nil {
		page = len(aws.ToString(input.ContinuationToken))
	}
	output := &s3.ListObjectsV2Output{}
	if page < len(f.pages) {
		output.Contents = f.pages[page]
	}
	if page+1 < len(f.pages) {
		// The token's length is the next page's index.
		token := make([]byte, page+1)
		for i := range token {
			token[i] = 'x'
		}
		output.IsTruncated = aws.Bool(true)
		output.NextContinuationToken = aws.String(string(token))
	}
	return output, nil
}
