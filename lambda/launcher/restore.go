package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// Restoring is the phase of a restore of ours: the host starts and its adapter puts an archive in place of the
// server's world, then a fresh start's record replaces it to start the game. The page shows it as starting.
const Restoring = "restoring"

const (
	// restoreDeadline bounds a restore this function began, from its request to the archive in place: the host's
	// start, the adapter's own download wait and the margins. A restore still unfinished after it is reported
	// failed. The start that follows has its own startDeadline.
	restoreDeadline = 45 * time.Minute
	// restoreExecutionSeconds is how long the host's adapter may take to download, check and unpack the archive.
	restoreExecutionSeconds = 1800
)

// archiveName is the name an archive of the server has in S3, beside its .sha256: the UTC time it was made.
var archiveName = regexp.MustCompile(`^[0-9]{8}T[0-9]{6}Z\.tar\.gz$`)

// ErrBadRestoreRequest means the request names no archive.
var ErrBadRestoreRequest = errors.New("bad restore request")

// Restore puts the named archive of the server back in place of its world, then starts the game. It runs on a
// stopped server, or a running host whose start or restore of ours failed on this run; anything else is left
// alone and answered with its status, so the operator stops the server first.
func (l *Launcher) Restore(ctx context.Context, archive string) (Status, bool, error) {
	if l.Config.HostID == "" {
		return Status{}, false, ErrNoServer
	}
	if !archiveName.MatchString(archive) {
		return Status{}, false, fmt.Errorf("%w: an archive is named for the time it was made, such as 20261016T031500Z.tar.gz", ErrBadRestoreRequest)
	}
	key := l.restoreKey(archive)
	h, err := l.describeHost(ctx)
	if err != nil {
		return l.unknown(ctx), false, err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return l.answer(Unknown, unreadable), false, err
	}
	now := l.Now().UTC()
	switch h.state {
	case ec2types.InstanceStateNameStopped:
		if (session.Phase == Starting || session.Phase == Restoring) && now.Sub(session.RequestedAt) < stoppedGrace {
			// A start or restore of ours was just recorded and EC2's description still lags it: that request
			// stands, rather than two follow-ups running one record at once.
			return l.answer(Starting, session), false, nil
		}
		// Record the restore before EC2 sees it, so every status read from here on knows it is under way.
		session = Session{RequestedAt: now, Phase: Restoring, RestoreKey: key}
		if err := l.saveSession(ctx, session); err != nil {
			return l.answer(Unknown, Session{}), false, err
		}
		return l.answer(Starting, session), true, errors.Join(l.startHost(ctx), l.followUp(ctx))
	case ec2types.InstanceStateNameRunning:
		state, err := l.decide(ctx, h, session)
		if err != nil {
			return l.answer(Unknown, l.current(h, session)), false, err
		}
		if state != Failed {
			return l.answer(state, l.current(h, session)), false, nil
		}
		// The host runs, but a start or restore of ours failed on this run, so no game is up: restore on the
		// same run.
		session = Session{RequestedAt: now, LaunchTime: h.launchTime, Phase: Restoring, RestoreKey: key}
		if err := l.saveSession(ctx, session); err != nil {
			return l.answer(Unknown, Session{}), false, err
		}
		return l.answer(Starting, session), true, l.followUp(ctx)
	default:
		// Pending, stopping and anything else: a transition is under way.
		state, err := l.decide(ctx, h, session)
		if err != nil {
			return l.answer(Unknown, l.current(h, session)), false, err
		}
		return l.answer(state, l.current(h, session)), false, nil
	}
}

// restoreKey answers the S3 key under which the host's adapter keeps the named archive of this server.
func (l *Launcher) restoreKey(archive string) string {
	return "instances/" + l.Config.Instance + "/crafty/" + archive
}

// finishRestore does the rest of a restore once EC2 has accepted its start: a start's steps up to the game,
// then the host adapter's restore of the archive. A run that Lambda ends before the restore's own deadline
// hands the rest to a fresh run of itself; the record carries the restore's command id, so that run follows
// the same restore. Once the archive is in place, a fresh start's record replaces the restore's.
func (l *Launcher) finishRestore(ctx context.Context, session Session) error {
	deadline := session.RequestedAt.Add(restoreDeadline - finishMargin)
	if !l.Now().Before(deadline) {
		return l.fail(ctx, session, errors.New("the restore passed its deadline before its follow-up ran"))
	}
	runCtx, cancel := context.WithTimeout(ctx, deadline.Sub(l.Now()))
	defer cancel()
	err := l.restoreSteps(runCtx, &session, deadline)
	switch {
	case err == nil:
		return l.startRestored(ctx, session)
	case errors.Is(err, errFinished):
		return nil
	case ctx.Err() != nil && l.Now().Before(deadline):
		// This run's own time ended first: another run takes it from here.
		handoff, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		if err := l.FollowUp(handoff); err != nil {
			return l.fail(handoff, session, fmt.Errorf("hand off the rest of the restore: %w", err))
		}
		return nil
	}
	return l.fail(ctx, session, err)
}

func (l *Launcher) restoreSteps(ctx context.Context, session *Session, deadline time.Time) error {
	h, err := l.waitRunning(ctx, session.RequestedAt)
	if err != nil {
		return err
	}
	saved, err := l.loadSession(ctx)
	if err != nil {
		return err
	}
	_, ours := l.sessionFor(h, saved)
	switch {
	case !saved.RequestedAt.Equal(session.RequestedAt):
		return errors.New("a later request owns the record")
	case finished(saved):
		return errFinished // a repeated run found the restore already finished
	case !ours || saved.Phase != Restoring:
		return errors.New("no restore of this function is under way for the running host")
	}
	*session = saved
	session.LaunchTime = h.launchTime
	// DNS goes first, as for a start, so the record propagates while the archive unpacks. Its failure never
	// stops the restore: the start that follows points the hostname again and reports it.
	session.DNS = "current"
	if err := l.PointDNS(ctx, h.publicIP); err != nil {
		session.DNS = "failed"
	}
	if err := l.saveOwned(ctx, *session); err != nil {
		return err
	}
	if err := l.waitSSM(ctx); err != nil {
		return err
	}
	if session.RestoreCommandID == "" {
		// The key goes into the command in single quotes, which keep it literal: it must name one of this
		// server's archives, and loadConfig checked the instance's name, so it holds only lower-case letters,
		// digits, T, Z, hyphens, slashes and dots.
		if archive, ok := strings.CutPrefix(session.RestoreKey, l.restoreKey("")); !ok || !archiveName.MatchString(archive) {
			return errors.New("the restore's record names no archive of this server")
		}
		// Delivery and execution together end before the restore's deadline, so the remote restore never
		// outlives the restore it belongs to and overlaps a retry.
		timeout := min(restoreExecutionSeconds, int(deadline.Sub(l.Now()).Seconds())-commandDelivery-15)
		if timeout < 120 {
			return errors.New("too little time is left to restore the archive")
		}
		// The adapter's installation comes first, so the host restores with the adapter this launcher carries.
		command := l.onHost(remoteCommand + " restore " + l.Config.Instance + " --key '" + session.RestoreKey + "'")
		id, err := l.sendCommand(ctx, command, "minecraft launcher restore", timeout)
		if err != nil {
			return fmt.Errorf("send restore: %w", err)
		}
		session.RestoreCommandID = id
		session.RestoreSentAt = l.Now().UTC()
		if err := l.saveOwned(ctx, *session); err != nil {
			return err
		}
	}
	return l.followRestore(ctx, *session)
}

// followRestore waits for the restore to finish and checks the adapter's answer, the way followDrain follows a
// drain. A restore that fails, or vanishes, fails with the adapter's own reason when it gave one.
func (l *Launcher) followRestore(ctx context.Context, session Session) error {
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
			CommandId: aws.String(session.RestoreCommandID), InstanceId: aws.String(l.Config.HostID),
		})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
			if l.Now().Sub(session.RestoreSentAt) > invisibleGrace {
				return errors.New("the restore never became visible")
			}
		case err != nil:
			return fmt.Errorf("follow restore: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			return checkRestored(aws.ToString(invocation.StandardOutputContent), invocation.ResponseCode, l.Config.Instance, session.RestoreKey)
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return restoreFailure(aws.ToString(invocation.StandardOutputContent), fmt.Sprintf("the restore ended with status %s", invocation.Status))
		}
		if err := l.pause(ctx, "the restore did not finish"); err != nil {
			return err
		}
	}
}

// checkRestored reads the adapter's answer: one object reporting the archive asked for in place for the
// configured instance.
func checkRestored(output string, code int32, instance, key string) error {
	if code != 0 {
		return restoreFailure(output, "the host's adapter refused the restore")
	}
	var result struct {
		Instance string `json:"instance"`
		Restored string `json:"restored"`
	}
	if json.Unmarshal([]byte(output), &result) != nil || result.Instance != instance || result.Restored != key {
		return restoreFailure(output, "the host's adapter did not confirm the restore")
	}
	return nil
}

// restoreFailure answers the adapter's own reason for a failed restore, which tells the operator what to do,
// or fallback when it gave none.
func restoreFailure(output, fallback string) error {
	if reason := adapterError(output); reason != "" {
		return errors.New("restore the archive" + reason)
	}
	return errors.New(fallback)
}

// startRestored replaces the restore's record, while the restore still owns it, with a fresh start's on the
// same run of the host, and hands the game's start to a fresh run, which has a start's whole time, as a
// start's retry on a running host does. It keeps a moment of its own, so a restore that finished just as this
// run's time ended still starts the game.
func (l *Launcher) startRestored(ctx context.Context, restore Session) error {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()
	start := Session{RequestedAt: l.Now().UTC(), LaunchTime: restore.LaunchTime, Phase: Starting, DNS: restore.DNS}
	if err := l.replaceOwned(ctx, restore.RequestedAt, start); errors.Is(err, errFinished) {
		return nil
	} else if err != nil {
		return l.fail(ctx, restore, fmt.Errorf("record the start after the restore: %w", err))
	}
	if err := l.followUp(ctx); err != nil {
		return l.fail(ctx, start, err)
	}
	return nil
}
