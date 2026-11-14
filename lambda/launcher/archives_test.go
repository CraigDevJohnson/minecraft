package main

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
)

func object(key string, size int64, modified time.Time) s3types.Object {
	return s3types.Object{Key: aws.String(key), Size: aws.Int64(size), LastModified: aws.Time(modified)}
}

func TestArchivesListsAWorldsArchivesNewestFirst(t *testing.T) {
	w := newWorlds(t)
	older, newer := now.Add(-48*time.Hour), now.Add(-time.Hour)
	// S3 lists in key order, over pages; the .sha256 beside each archive and anything else under the prefix is
	// not an archive.
	w.bucket.pages = [][]s3types.Object{
		{
			object("instances/creative/crafty/20261009T150000Z.tar.gz", 100, older),
			object("instances/creative/crafty/20261009T150000Z.tar.gz.sha256", 90, older),
		},
		{
			object("instances/creative/crafty/20261011T150000Z.tar.gz", 200, newer.In(time.FixedZone("PDT", -7*3600))),
			object("instances/creative/crafty/20261011T150000Z.tar.gz.sha256", 90, newer),
			object("instances/creative/crafty/notes.txt", 5, newer),
		},
	}
	list, err := w.worlds.Archives(context.Background(), "creative")
	if err != nil {
		t.Fatal(err)
	}
	want := ArchiveList{World: "creative", Archives: []Archive{
		{Name: "20261011T150000Z.tar.gz", Size: 200, Modified: newer.UTC()},
		{Name: "20261009T150000Z.tar.gz", Size: 100, Modified: older.UTC()},
	}}
	if fmt.Sprint(list) != fmt.Sprint(want) || list.Archives[0].Modified.Location() != time.UTC {
		t.Fatalf("list %+v", list)
	}
	// Only that world's own archives are asked for, in the bucket the function was given.
	if len(w.bucket.asked) != 2 || aws.ToString(w.bucket.asked[0].Bucket) != backupBucket || aws.ToString(w.bucket.asked[0].Prefix) != "instances/creative/crafty/" {
		t.Fatalf("asked %+v", w.bucket.asked)
	}
}

func TestArchivesListsAtMostFifty(t *testing.T) {
	w := newWorlds(t)
	page := []s3types.Object{}
	for day := range 60 {
		made := now.Add(time.Duration(day-60) * 24 * time.Hour)
		page = append(page, object("instances/survival/crafty/"+made.Format("20060102T150405Z")+".tar.gz", 1, made))
	}
	w.bucket.pages = [][]s3types.Object{page}
	list, err := w.worlds.Archives(context.Background(), "survival")
	if err != nil || len(list.Archives) != maxArchives {
		t.Fatalf("%d archives, err %v", len(list.Archives), err)
	}
	if newest := now.Add(-24 * time.Hour).Format("20060102T150405Z"); list.Archives[0].Name != newest+".tar.gz" {
		t.Fatalf("first %s", list.Archives[0].Name)
	}
}

func TestArchivesOfAWorldWithoutAnyIsEmpty(t *testing.T) {
	w := newWorlds(t)
	list, err := w.worlds.Archives(context.Background(), "survival")
	if err != nil || list.World != "survival" || list.Archives == nil || len(list.Archives) != 0 {
		t.Fatalf("list %+v err %v", list, err)
	}
}

func TestArchivesRefusesAnUnknownWorldAndReportsS3(t *testing.T) {
	w := newWorlds(t)
	if _, err := w.worlds.Archives(context.Background(), "nether"); !errors.Is(err, ErrUnknownWorld) || len(w.bucket.asked) != 0 {
		t.Fatalf("err %v, asked %v", err, w.bucket.asked)
	}
	w.bucket.err = errors.New("AccessDenied")
	if _, err := w.worlds.Archives(context.Background(), "survival"); err == nil {
		t.Fatal("a listing S3 refused must fail")
	}
}
