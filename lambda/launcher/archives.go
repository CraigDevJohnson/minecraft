package main

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// maxArchives is how many of a world's archives GET /server/archives lists, the newest first. The host adapter
// keeps fourteen, so the list is normally whole.
const maxArchives = 50

// ArchiveList is the answer to GET /server/archives: one world's archives in the backup bucket, newest first.
type ArchiveList struct {
	World    string    `json:"world"`
	Archives []Archive `json:"archives"`
}

// Archive is one archive of a world. Name is what POST /server/restore takes as its archive.
type Archive struct {
	Name     string    `json:"name"`
	Size     int64     `json:"size"`
	Modified time.Time `json:"modified"`
}

// Archives lists the archives the host adapter stored for a world under instances/<world>/crafty/ in the backup
// bucket: the .tar.gz objects named for the time they were made, not the .sha256 beside each.
func (w *Worlds) Archives(ctx context.Context, world string) (ArchiveList, error) {
	launcher, ok := w.Launchers[world]
	if !ok {
		return ArchiveList{}, ErrUnknownWorld
	}
	prefix := launcher.restoreKey("")
	pages := s3.NewListObjectsV2Paginator(w.S3, &s3.ListObjectsV2Input{Bucket: aws.String(w.BackupBucket), Prefix: aws.String(prefix)})
	archives := []Archive{}
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return ArchiveList{}, fmt.Errorf("list the archives: %w", err)
		}
		for _, object := range page.Contents {
			name, _ := strings.CutPrefix(aws.ToString(object.Key), prefix)
			if !archiveName.MatchString(name) {
				continue
			}
			archive := Archive{Name: name, Size: aws.ToInt64(object.Size)}
			if object.LastModified != nil {
				archive.Modified = object.LastModified.UTC()
			}
			archives = append(archives, archive)
		}
	}
	// Each name is the UTC time its archive was made, so the names sort as the archives were made.
	slices.SortFunc(archives, func(a, b Archive) int { return strings.Compare(b.Name, a.Name) })
	return ArchiveList{World: world, Archives: archives[:min(len(archives), maxArchives)]}, nil
}
