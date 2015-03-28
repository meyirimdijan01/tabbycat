# cannot import debate.models here - would create circular dep
from django.core.exceptions import ObjectDoesNotExist

class Scoresheet(object):
    """Representation of a single adjudicator's scoresheet in a single ballot
    submission, providing an interface that abstracts away database operations.
    Each instance initializes itself with the appropriate data on construction.

    The interface to the client (views and forms) uses Teams and Adjudicators
    (not DebateTeams and DebateAdjudicators), and raises a DoesNotExist error if
    a team or adjudicator not associated with the debate is supplied. Instead,
    'aff' and 'neg' can also be used to specify teams, but this will only work
    if team positions are already known.

    This class does *not* deal with any information about the *identity* of the
    speakers involved. That is all done in the BallotSet class, which comprises
    (among other things) one or more instances of this class. This class merely
    stores raw scores awarded by a particular adjudicator (in a particular
    submission), attaching scores to positions.

    Internally, scores are stored in a nested dictionary self.data, such that
        self.data[debateteam][pos] = score.
    This is known as the "buffer"."""

    def __init__(self, ballotsub, adjudicator):
        self.ballotsub = ballotsub
        self.debate = ballotsub.debate
        self.adjudicator = adjudicator
        self.da = self.debate.debateadjudicator_set.get(adjudicator=adjudicator)
        self.dts = self.debate.debateteam_set.all() # note, this is a QuerySet
        self.POSITIONS = self.debate.round.tournament.POSITIONS
        self.data = {dt: self._no_scores() for dt in self.dts}
        for dt in self.dts:
            self._load_team(dt)

    @property
    def is_complete(self):
        return all(all(self.data[dt][p] is not None for p in self.POSITIONS) \
                for dt in self.dts)

    def save(self):
        """Saves the information in this instance to the database."""
        assert self.is_complete, "Tried to save scoresheet when it is incomplete"
        self.ballotsub.speakerscorebyadj_set.filter(debate_adjudicator=self.da).delete()
        for dt in self.dts:
            self._save_team(dt)

    def _no_scores(self):
        return dict.fromkeys(self.POSITIONS, None)

    def _load_team(self, dt):
        """Loads the scores for the given DebateTeam from the database into the
        buffer."""
        scores = self.ballotsub.speakerscorebyadj_set.filter(
                debate_adjudicator=self.da, debate_team=dt)
        for ss in scores:
            self._set_score(dt, ss.position, ss.score)

    def _save_team(self, dt):
        """Saves the scores in the buffer for the given DebateTeam, to the
        database."""
        from debate.models import SpeakerScoreByAdj
        for pos in self.POSITIONS:
            SpeakerScoreByAdj(
                ballot_submission=self.ballotsub,
                debate_adjudicator=self.da,
                debate_team=dt,
                position=pos,
                score=self._get_score(dt, pos),
            ).save()

    def _set_score(self, dt, position, score):
        self.data[dt][position] = score

    def _get_score(self, dt, position):
        return self.data[dt][position]

    def _get_total(self, dt):
        """Returns the total for the given DebateTeam, or None if the scores
        are incomplete."""
        scores = [self.data[dt][p] for p in self.POSITIONS]
        if None in scores:
            return None
        return sum(scores)

    def _get_dt(self, team):
        """Extracts a DebateTeam from a given team argument. The argument can be
        either a Team or 'aff'/'neg'."""
        if team in ['aff', 'neg']:
            return self.debate.get_dt(team)
        try:
            return self.dts.get(team=team)
        except ObjectDoesNotExist:
            raise ValueError("The team %s is not in the debate for this scoresheet." % team)

    def _get_winner(self):
        """Returns the winner as a DebateTeam, or None if scoresheet is
        incomplete or if it is a draw."""
        if not self.is_complete:
            return None
        dts = list(self.dts) # fix order for loops
        totals = [self._get_total(dt) for dt in dts]
        max_total = max(totals)
        if totals.count(max_total) > 1:
            return None
        for dt, total in zip(dts, totals):
            if total == max_total:
                return dt
        raise RuntimeError("Unexpected error") # this should never happen

    def set_score(self, team, position, score):
        """Sets the score for the given team and position in the data buffer,
        not saved to database."""
        return self._set_score(self._get_dt(team), position, score)

    def get_score(self, team, position):
        """Returns the score for the given team and position, reading from the
        data buffer."""
        return self._get_score(self._get_dt(team), position) or 0 # don't return None

    def get_total(self, team):
        """Returns the team score for the given team, reading from the data
        buffer."""
        return self._get_total(self._get_dt(team))

    @property
    def winner(self):
        """Returns the winner as a Team object."""
        return self._get_winner().team

    @property
    def aff_score(self):
        return self.get_total(self.debate.aff_dt)

    @property
    def neg_score(self):
        return self.get_total(self.debate.neg_dt)

    @property
    def aff_win(self):
        return self.aff_score > self.neg_score

    @property
    def neg_win(self):
        return self.neg_score > self.aff_score

class BallotSet(object):
    """Representation of a set of ballots for a debate in a single ballot
    submission, providing an interface that abstracts away database operations.
    In particular, this class makes it easier for views and forms to work with a
    set of ballots, acting as a translation layer on top of the
    BallotSubmission, TeamScore, SpeakerScore and Motion models. Each instance
    initializes itself with the appropriate data on construction.

    The interface to the client (views and forms) uses Teams and Adjudicators
    (not DebateTeams and DebateAdjudicators), and raises a DoesNotExist error if
    a team or adjudicator not associated with the debate is supplied. Instead,
    'aff' and 'neg' can also be used to specify teams, but this will only work
    if team positions are already known.

    Specifcally, this class performs the following (non-trivial) functions:
      - Keeps track of which speaker spoke in which position.
      - Figures out which adjudicators are in the majority.
      - Calculates the majority-average speaker scores.
    """

    def __init__(self, ballotsub):
        """Constructor.
        'ballots' must be a BallotSubmission.
        """
        self.ballotsub = ballotsub
        self.debate = ballotsub.debate
        self.adjudicators = self.debate.adjudicators.list
        self.dts = self.debate.debateteam_set.all() # note, this is a QuerySet
        assert self.dts.count() == 2, "There aren't two DebateTeams in this debate: %s." % self.debate
        self.POSITIONS = self.debate.round.tournament.POSITIONS

        self.loaded_sheets = False
        self._adjudicator_sheets = None

        self.speakers    = {dt: {} for dt in self.dts}
        self.points      = dict.fromkeys(self.dts, None)
        self.total_score = dict.fromkeys(self.dts, None)
        self.wins        = dict.fromkeys(self.dts, None)
        self.margins     = dict.fromkeys(self.dts, None)
        self.motion_veto = dict.fromkeys(self.dts, None)

        self._other = {self.dts[0]: self.dts[1], self.dts[1]: self.dts[0]}

        for dt in self.dts:
            self._load_team(dt)

    def _get_dt(self, team):
        """General-purpose function for extracting a DebateTeam from a given
        team argument. The argument can be either a Team or 'aff'/'neg'."""
        if team in ['aff', 'neg']:
            return self.debate.get_dt(team)
        else:
            return self.dts.get(team=team)

    def _load_team(self, dt):
        """Loads the scores for the given DebateTeam from the database into the
        buffer."""
        for ss in self.ballotsub.speakerscore_set.filter(debate_team=dt):
            self.speakers[dt][ss.position] = ss.speaker
            # ignore the speaker score itself, just look at SpeakerScoreByAdjs

        try:
            ts = self.ballotsub.teamscore_set.get(debate_team=dt)
            self.points[dt] = ts.points
            self.total_score[dt] = ts.score
            self.wins[dt] = ts.win
            self.margins[dt] = ts.margin
        except ObjectDoesNotExist:
            self.points[dt] = None
            self.total_score[dt] = None
            self.wins[dt] = None
            self.margins[dt] = None

        try:
            dtmp = self.ballotsub.debateteammotionpreference_set.get(
                    debate_team=dt, preference=3)
            self.motion_veto[dt] = dtmp.motion
        except ObjectDoesNotExist:
            self.motion_veto[dt] = None

    @property
    def is_complete(self):
        return all(sheet.is_complete for sheet in self.adjudicator_sheets.itervalues())

    @property
    def adjudicator_sheets(self):
        if not self._adjudicator_sheets:
            self._adjudicator_sheets = {a: Scoresheet(self.ballotsub, a)
                    for a in self.adjudicators}
            self.loaded_sheets = True
        return self._adjudicator_sheets

    def save(self):
        assert self.is_complete, "Tried to save ballot set when it is incomplete"

        self.ballotsub.save()
        for sheet in self.adjudicator_sheets.itervalues():
            sheet.save()
        self._calc_decision()
        for dt in self.dts:
            self._save_team(dt)

    def _calc_decision(self):
        """Calculates the majority decision and puts the majority adjudicators
        in self._majority_adj and the winning DebateTeam in self._winner. Does
        nothing if scores are incomplete or if it looks like a draw."""
        if not self.is_complete:
            return

        adjs_by_dt = {dt: [] for dt in self.dts} # group adjs by vote
        for adj, sheet in self.adjudicator_sheets.iteritems():
            winner = sheet._get_winner()
            adjs_by_dt[winner].append(adj)

        counts = {dt: len(adjs) for dt, adjs in adjs_by_dt.iteritems()}
        max_count = max(counts.values()) # check that we have a majority
        if max_count < len(self.adjudicators) / 2 + 1:
            return

        for dt, count in counts.iteritems(): # set self._majority_adj
            if count == max_count:
                self._majority_adj = adjs_by_dt[dt]
                self._winner = dt
                break

    @property
    def majority_adj(self):
        if not self.is_complete:
            return []
        try:
            return self._majority_adj
        except AttributeError:
            self._calc_decision()
            return self._majority_adj

    @property
    def winner(self):
        """Returns the winner of the debate as a Team object."""
        if not self.is_complete:
            return None
        try:
            return self._winner.team
        except AttributeError:
            self._calc_decision()
            return self._winner.team

    def _save_team(self, dt):
        from debate.models import TeamScore, SpeakerScore, DebateTeamMotionPreference

        total = self._get_avg_total(dt)
        points = self._get_points(dt)
        win = self._get_win(dt)
        margin = self._get_margin(dt)

        TeamScore.objects.filter(ballot_submission=self.ballotsub, debate_team=dt).delete()
        TeamScore(ballot_submission=self.ballotsub,
                  debate_team=dt, score=total, points=points, win=win,
                  margin=margin).save()

        SpeakerScore.objects.filter(ballot_submission=self.ballotsub, debate_team=dt).delete()
        for pos in self.POSITIONS:
            speaker = self.speakers[dt][pos]
            score = self._get_avg_score(dt, pos)
            SpeakerScore(
                ballot_submission = self.ballotsub,
                debate_team = dt,
                speaker = speaker,
                score = score,
                position = pos,
            ).save()

        DebateTeamMotionPreference.objects.filter(ballot_submission=self.ballotsub, debate_team=dt, preference=3).delete()
        if self.motion_veto[dt] is not None:
            DebateTeamMotionPreference(ballot_submission=self.ballotsub, debate_team=dt, preference=3, motion=self.motion_veto[dt]).save()

        self.ballotsub.save()

    def _get_speaker(self, dt, position):
        return self.speakers[dt].get(position)

    def _get_score(self, adj, dt, position):
        return self.adjudicator_sheets[adj]._get_score(dt, position)

    def _get_avg_score(self, dt, position):
        if not self.is_complete:
            return None
        return sum(self.adjudicator_sheets[adj]._get_score(dt, position)
                   for adj in self.majority_adj) / len(self.majority_adj)

    def _set_speaker(self, dt, position, speaker):
        self.speakers[dt][position] = speaker

    def _set_score(self, adj, dt, position, score):
        self.adjudicator_sheets[adj]._set_score(dt, position, score)

    def _get_avg_total(self, dt):
        if not self.loaded_sheets:
            return self.total_score[dt]
        return sum(self.adjudicator_sheets[adj]._get_total(dt) for adj in
                   self.majority_adj) / len(self.majority_adj)

    def _dissenting_inclusive_score(self, dt):
        dissenting_score = sum(self.adjudicator_sheets[adj]._get_total(dt) for adj in
                   self.adjudicators) / len(self.adjudicators)
        return dissenting_score

    # Abstracted to not be tied to wins
    def _get_points(self, dt):
        if not self.loaded_sheets:
            return self.points[dt]

        if self._get_avg_total(dt):
            if self._get_avg_total(dt) > self._get_avg_total(self._other[dt]):
                return 1
            return 0

        return None

    # Supplants _points; ie its a count of the number of wins
    def _get_win(self, dt):
        if not self.loaded_sheets:
            return self.win[dt]

        if self._get_avg_total(dt):
            if self._get_avg_total(dt) > self._get_avg_total(self._other[dt]):
                return True
            return False

        return None

    def _get_margin(self, dt):
        if not self.loaded_sheets:
            return self.margin[dt]

        if self.debate.round.tournament.config.get('margin_includes_dissenters') is False:
            if self._get_avg_total(dt) and self._get_avg_total(self._other[dt]):
                return self._get_avg_total(dt) - self._get_avg_total(self._other[dt])
        else:
            if self._dissenting_inclusive_score(dt) and self._dissenting_inclusive_score(self._other[dt]):
                dissenting_inclusive_margin = self._dissenting_inclusive_score(dt) - self._dissenting_inclusive_score(self._other[dt])
                return dissenting_inclusive_margin

        return None

    def get_speaker(self, team, position):
        """Returns the speaker object for team/position."""
        return self._get_speaker(self._get_dt(team), position)

    def get_score(self, adj, team, position):
        """Returns the score given by the adjudicator for the speaker in this
        team and position."""
        return self._get_score(adj, self._get_dt(team), position)

    def get_avg_score(self, team, position):
        """Returns the average score of majority adjudicators for this team and
        position."""
        return self._get_avg_score(self._get_dt(team), position)

    def set_speaker(self, team, position, speaker):
        """Sets the identity of the speaker in this team and position."""
        return self._set_speaker(self._get_dt(team), position, speaker)

    def set_score(self, adj, team, position, score):
        """Set the score given by adjudicator for this team and position."""
        return self._set_score(adj, self._get_dt(team), position, score)

    @property
    def aff_score(self):
        return self._get_avg_total(self.debate.aff_dt)

    @property
    def neg_score(self):
        return self._get_avg_total(self.debate.neg_dt)

    @property
    def aff_points(self):
        return self._get_points(self.debate.aff_dt)

    @property
    def neg_points(self):
        return self._get_points(self.debate.neg_dt)

    @property
    def aff_win(self):
        return self.aff_points

    @property
    def neg_win(self):
        return self.neg_points

    def is_trainee(self, adj):
        from debate import models as m
        da = m.DebateAdjudicator.objects.get(
            adjudicator = adj,
            debate = self.debate)
        return da.type == m.DebateAdjudicator.TYPE_TRAINEE

    @property
    def adjudicator_results(self):
        # TODO change this to use self.adjudicators not self.debate.adjudicators
        self._calc_decision()
        splits = [adj not in self.majority_adj and not self.is_trainee(adj)
                for _, adj in self.debate.adjudicators]
        for (type, adj), split in zip(self.debate.adjudicators, splits):
            yield type, adj, split

    @property
    def confirmed(self):
        return self.ballotsub.confirmed

    @confirmed.setter
    def confirmed(self, new):
        self.ballotsub.confirmed = new

    @property
    def discarded(self):
        return self.ballotsub.discarded

    @discarded.setter
    def discarded(self, new):
        self.ballotsub.discarded = new

    @property
    def motion(self):
        return self.ballotsub.motion

    @motion.setter
    def motion(self, new):
        self.ballotsub.motion = new

    @property
    def aff_motion_veto(self):
        return self.motion_veto[self.debate.aff_dt]

    @aff_motion_veto.setter
    def aff_motion_veto(self, new):
        self.motion_veto[self.debate.aff_dt] = new

    @property
    def neg_motion_veto(self):
        return self.motion_veto[self.debate.neg_dt]

    @neg_motion_veto.setter
    def neg_motion_veto(self, new):
        self.motion_veto[self.debate.neg_dt] = new

    @property
    def sheet_iter(self):
        """Usage:

        for sheet in ballotset.sheet_iter:
            print sheet.adjudicator
            for pos in sheet.affs:
                print pos.name, pos.speaker, pos.score
            print sheet.aff_score, sheet.aff_win
            for pos in sheet.negs:
                print pos.name, pos.speaker, pos.score
            print sheet.neg_score, sheet.neg_win
        """
        REPLY_POSITION = self.debate.round.tournament.REPLY_POSITION
        POSITIONS = self.debate.round.tournament.POSITIONS

        class Position(object):

            def __init__(self2, sheet, side, pos):
                self2.sheet = sheet
                self2.pos = pos
                self2.side = side

            @property
            def name(self2):
                return "Reply" if (self2.pos == REPLY_POSITION) else str(self2.pos)

            @property
            def speaker(self2):
                return self.get_speaker(self2.side, self2.pos)

            @property
            def score(self2):
                return self2.sheet.get_score(self2.side, self2.pos)

        class ScoresheetWrapper(object):

            def __init__(self2, adj):
                self2.sheet = self.adjudicator_sheets[adj]
                self2.adjudicator = adj

            def position_iter(self2, side):
                for pos in POSITIONS:
                    yield Position(self2.sheet, side, pos)

            @property
            def affs(self2):
                return self2.position_iter('aff')

            @property
            def negs(self2):
                return self2.position_iter('neg')

            @property
            def aff_score(self2):
                return self2.sheet.aff_score

            @property
            def neg_score(self2):
                return self2.sheet.neg_score

            @property
            def aff_win(self2):
                return self2.sheet.aff_win

            @property
            def neg_win(self2):
                return self2.sheet.neg_win

        for adj in self.adjudicators:
            yield ScoresheetWrapper(adj)


class ForfeitBallotSet(BallotSet):
    # This is WADL-specific for now

    def __init__(self, ballots, forfeiter):
        """Constructor.
        'ballots' must be a BallotSubmission.
        """
        self.ballotsub = ballots
        self.debate = ballots.debate
        self.adjudicators = self.debate.adjudicators.list
        self.forfeiter = forfeiter

    def save_side(self, dt):

        if self.forfeiter == dt:
            points = 0
            win = False
        else:
            points = 2
            win = True


        from debate.models import TeamScore
        # Note: forfeited debates have fake scores/margins, thus the affects_average toggle
        TeamScore.objects.filter(ballot_submission=self.ballotsub, debate_team=dt).delete()
        TeamScore(
            ballot_submission=self.ballotsub,
            debate_team=dt,
            points=points,
            win=win,
            score=0,
            margin=0,
            affects_averages=False).save()


    def save(self):
        self.ballotsub.forfeit = self.forfeiter
        self.ballotsub.save()
        for dt in self.dts:
            self.save_side(dt)


