from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils import formats
from django.utils.encoding import force_text
from django.utils.translation import ugettext as _
from django.utils.translation import ugettext_lazy, ungettext

from adjallocation.allocation import AdjudicatorAllocation
from adjallocation.utils import adjudicator_conflicts_display
from draw.models import Debate
from participants.models import Team
from standings.templatetags.standingsformat import metricformat, rankingformat
from tournaments.utils import get_side_name
from utils.misc import reverse_tournament
from venues.utils import venue_conflicts_display

from .mixins import SuperuserRequiredMixin


class BaseTableBuilder:
    """Class for building tables that can be easily inserted into Vue tables,
    Designed to be used with VueTableTemplateView.

    In the docstrings for this class:
    - A *header dict* is a dict that contains a value under `"key"` that is a
      string, and may optionally contain entries under `"tooltip"`, `"icon"`,
      `"visible-sm"`, `"visible-md"` and `"visible-lg"`.
    - A *cell dict* is a dict that contains a value under `"text"` that is a
      string, and may optionally contain entries under `"sort"`, `"icon"`,
      `"emoji"`, `"popover"` and `"link"`.

    """

    def __init__(self, **kwargs):
        self.headers = []
        self.data = []
        self.title = kwargs.get('title', "")
        self.table_class = kwargs.get('table_class', "")
        self.sort_key = kwargs.get('sort_key', '')
        self.sort_order = kwargs.get('sort_order', '')
        self.empty_title = kwargs.get('empty_title', _("No Data Available"))

    @staticmethod
    def _convert_header(header):
        if isinstance(header, dict):
            header['key'] = force_text(header['key'])
            return header
        else:
            return {'key': force_text(header)}

    @staticmethod
    def _convert_cell(cell):
        if isinstance(cell, dict):
            if 'text' in cell:
                cell['text'] = force_text(cell['text'])
            return cell
        else:
            cell_dict = {}
            if isinstance(cell, int) or isinstance(cell, float):
                cell_dict['sort'] = cell
            cell_dict['text'] = force_text(cell)
            return cell_dict

    def add_column(self, header, data):
        """Adds a column to the table.

        - `header` must be either a string or a header dict (see class docstring).
        - `data` must be a list of cells (in the column). Each cell should
          either be a string or a cell dict (see class docstring). If this is
          not the first column, then there must be as many elements in `data` as
          there are in the existing columns.
        """
        if len(self.data) > 0 and len(data) != len(self.data):
            raise ValueError("data contains {new:d} rows, existing table has {existing:d}".format(
                new=len(data), existing=len(self.data)))

        header = self._convert_header(header)
        self.headers.append(header)

        data = map(self._convert_cell, data)
        if len(self.data) == 0:
            self.data = [[cell] for cell in data]
        else:
            for row, cell in zip(self.data, data):
                row.append(cell)

    def add_boolean_column(self, header, data):
        """Convenience function for adding a column based on boolean data.

        - `header` must be either a string or a header dict.
        - `data` must be an iterable of booleans.
        """
        cells = [{
            'icon': 'check' if datum else '',
            'sort':  1 if datum else 2,
        } for datum in data]
        self.add_column(header, cells)

    def add_columns(self, headers, data):
        """Adds columns to the table.

        This method is intended for situations where it is easier to process
        data by row while adding it to the table, than it is to process data
        by column. In the latter case, use `add_column()` instead.

        - `headers` must be a list of strings or header dicts (or both).
        - `data` must be a list of lists of cells, where each cell is a string
          or cell dict. Each inner list corresponds to a row, and all inner
          lists must contain the same number of elements, which must also match
          the number of elements in `headers`. If there are no existing columns
          in the table, then there must be as many inner lists as there are
          existing columns.
        """
        if len(self.data) > 0 and len(data) != len(self.data):
            raise ValueError("data contains {new:d} rows, existing table has {existing:d}".format(
                new=len(data), existing=len(self.data)))

        headers = map(self._convert_header, headers)
        self.headers.extend(headers)

        if len(self.data) == 0:
            self.data = [[self._convert_cell(cell) for cell in row] for row in data]
        else:
            for row, cells in zip(self.data, data):
                cells = map(self._convert_cell, cells)
                row.extend(cells)

    def jsondict(self):
        """Returns the JSON dict for the table."""
        return {
            'head': self.headers,
            'data': self.data,
            'title': self.title,
            'empty_title': self.empty_title,
            'class': self.table_class,
            'sort_key': self.sort_key,
            'sort_order': self.sort_order,
        }


class TabbycatTableBuilder(BaseTableBuilder):
    """Extends TableBuilder to add convenience functions specific to
    Tabbycat."""

    ADJ_SYMBOLS = {
        AdjudicatorAllocation.POSITION_CHAIR: _("Ⓒ"),
        AdjudicatorAllocation.POSITION_TRAINEE: _("Ⓣ"),
    }

    ADJ_POSITION_NAMES = {
        AdjudicatorAllocation.POSITION_CHAIR: _("chair"),
        AdjudicatorAllocation.POSITION_PANELLIST: _("panellist"),
        AdjudicatorAllocation.POSITION_TRAINEE: _("trainee"),
    }

    BLANK_TEXT = "—"

    def __init__(self, view=None, **kwargs):
        """Constructor.
        - If `tournament` is specified, it becomes the default tournament for
          the builder.
        - If `admin` is True (default is False), then relevant links will go
          to the admin version rather than the public version.
        - If `view` is specified, then `tournament` and `admin` are inferred
          from `view`. This option is provided for convenience.
        """
        if 'tournament' not in kwargs and hasattr(view, 'get_tournament'):
            self.tournament = view.get_tournament()
        else:
            self.tournament = kwargs.get('tournament')

        if 'admin' not in kwargs and isinstance(view, SuperuserRequiredMixin) or \
                (isinstance(view, LoginRequiredMixin) and view.request.user.is_superuser):
            self.admin = True
        else:
            self.admin = kwargs.get('admin', False)

        if self.tournament.pref('teams_in_debate') == 'bp':
            self._result_cell = self._result_cell_bp
        else:
            self._result_cell = self._result_cell_two

        return super().__init__(**kwargs)

    @property
    def _show_record_links(self):
        return self.admin or self.tournament.pref('public_record')

    @property
    def _show_speakers_in_draw(self):
        return self.tournament.pref('show_speakers_in_draw') or self.admin

    def _adjudicator_record_link(self, adj):
        adj_short_name = adj.name.split(" ")[0]
        if self.admin:
            return {
                'text': _("View %(adj)s's Adjudication Record") % {'adj': adj_short_name},
                'link': reverse_tournament('participants-adjudicator-record',
                    self.tournament, kwargs={'pk': adj.pk})
            }
        elif self.tournament.pref('public_record'):
            return {
                'text': _("View %(adj)s's Adjudication Record") % {'adj': adj_short_name},
                'link': reverse_tournament('participants-public-adjudicator-record',
                    self.tournament, kwargs={'pk': adj.pk})
            }
        else:
            return {'text': '', 'link': False}

    def _team_record_link(self, team):
        if self.admin:
            return {
                'text': _("View %(team)s's Team Record") % {'team': team.short_name},
                'link': reverse_tournament('participants-team-record', self.tournament, kwargs={'pk': team.pk})
            }
        elif self.tournament.pref('public_record'):
            return {
                'text': _("View %(team)s's Team Record") % {'team': team.short_name},
                'link': reverse_tournament('participants-public-team-record', self.tournament, kwargs={'pk': team.pk})
            }
        else:
            return {'text': '', 'link': False}

    def _team_cell(self, team, hide_emoji=True, subtext=None):
        cell = {
            'text': team.short_name,
            'emoji': team.emoji if self.tournament.pref('show_emoji') and not hide_emoji else None,
            'sort': team.short_name,
            'class': 'team-name',
            'popover': {'title': team.long_name, 'content': []}
        }
        if subtext:
            cell['subtext'] = subtext
        if self._show_speakers_in_draw:
            cell['popover']['content'].append({'text': ", ".join([s.name for s in team.speakers])})
        if self._show_record_links:
            cell['popover']['content'].append(self._team_record_link(team))
        return cell

    def _result_cell_class_two(self, win, cell):
        team_name = cell['popover']['title']
        if win is True:
            cell['popover']['title'] = _("%(team)s won") % {'team': team_name}
            cell['icon'] = "chevron-up"
            cell['iconClass'] = "text-success"
            cell['sort'] = 2
        elif win is False:
            cell['popover']['title'] = _("%(team)s lost") % {'team': team_name}
            cell['icon'] = "chevron-down"
            cell['iconClass'] = "text-danger"
            cell['sort'] = 1
        else: # None
            cell['popover']['title'] = _("%(team)s—no result") % {'team': team_name}
            cell['icon'] = ""
            cell['sort'] = 0
        return cell

    def _result_cell_class_four(self, points, cell):
        team_name = cell['popover']['title']
        if points is 3:
            cell['popover']['title'] = _("%(team)s took 1st") % {'team': team_name}
            cell['icon'] = "chevrons-up"
            cell['iconClass'] = "text-success"
            cell['sort'] = 4
        elif points is 2:
            cell['popover']['title'] = _("%(team)s took 2nd") % {'team': team_name}
            cell['icon'] = "chevron-up"
            cell['iconClass'] = "text-info"
            cell['sort'] = 3
        elif points is 1:
            cell['popover']['title'] = _("%(team)s took 3rd") % {'team': team_name}
            cell['icon'] = "chevron-down"
            cell['iconClass'] = "text-warning"
            cell['sort'] = 2
        elif points is 0:
            cell['popover']['title'] = _("%(team)s took 4th") % {'team': team_name}
            cell['icon'] = "chevrons-down"
            cell['iconClass'] = "text-danger"
            cell['sort'] = 1
        else: # None
            cell['popover']['title'] = _("%(team)s—no result") % {'team': team_name}
            cell['icon'] = ""
            cell['sort'] = 0
        return cell

    def _result_cell_class_four_elim(self, advancing, cell):
        team_name = cell['popover']['title']
        if advancing is True:
            cell['popover']['title'] = _("%(team)s is advancing") % {'team': team_name}
            cell['icon'] = "chevron-up"
            cell['iconClass'] = "text-success"
            cell['sort'] = 2
        elif advancing is False:
            cell['popover']['title'] = _("%(team)s was eliminated") % {'team': team_name}
            cell['icon'] = "chevron-down"
            cell['iconClass'] = "text-danger"
            cell['sort'] = 1
        else: # None
            cell['popover']['title'] = _("%(team)s—no result") % {'team': team_name}
            cell['icon'] = ""
            cell['sort'] = 0
        return cell

    def _result_cell_two(self, ts, compress=False, show_score=False, show_ballots=False):
        if not hasattr(ts, 'debate_team') or not hasattr(ts.debate_team.opponent, 'team'):
            return {'text': self.BLANK_TEXT}

        opp = ts.debate_team.opponent.team
        opp_vshort = '<i class="emoji">' + opp.emoji + '</i>' if opp.emoji else "…"

        cell = {
            'text': _(" vs %(opposition)s") % {'opposition': opp_vshort if compress else opp.short_name},
            'popover': {'content': [{'text': ''}], 'title': ''}
        }
        cell = self._result_cell_class_two(ts.win, cell)

        if ts.win is True:
            cell['popover']['title'] = _("Won against %(team)s") % {'team': opp.long_name}
        elif ts.win is False:
            cell['popover']['title'] = _("Lost to %(team)s") % {'team': opp.long_name}
        else: # None
            cell['popover']['title'] = _("No result for debate against %(team)s") % {'team': opp.long_name}

        if show_score:
            cell['subtext'] = metricformat(ts.score)
            cell['popover']['content'].append(
                {'text': _("Received <strong>%s</strong> team points") % metricformat(ts.score)})

        if show_ballots:
            cell['popover']['content'].append(
                {'text': _("View debate ballot"), 'link': reverse_tournament('results-public-scoresheet-view',
                    self.tournament, kwargs={'pk': ts.debate_team.debate.id})})

        if self._show_speakers_in_draw:
            cell['popover']['content'].append({'text': _("Speakers in <strong>%(opp)s</strong>: %(speakers)s") % {
                'opp': opp.short_name, 'speakers': ", ".join([s.name for s in opp.speakers])}})

        if self._show_record_links:
            cell['popover']['content'].append(
                self._team_record_link(opp))

        return cell

    def _result_cell_bp(self, ts, compress=False, show_score=False, show_ballots=False):
        if not hasattr(ts, 'debate_team'):
            return {'text': self.BLANK_TEXT}

        other_teams = {dt.side: dt.team.short_name for dt in ts.debate_team.debate.debateteam_set.all()}
        other_team_strs = [_("Teams in debate:")]
        for side in self.tournament.sides:
            if ts.debate_team.debate.sides_confirmed:
                line = _("%(team)s (%(side)s)") % {
                    'team': other_teams.get(side, "??"),
                    'side': get_side_name(self.tournament, side, 'abbr')
                }
            else:
                line = other_teams.get(side, "??")
            if side == ts.debate_team.side:
                line = "<strong>" + line + "</strong>"
            other_team_strs.append(line)

        cell = {'popover': {
            'content': [{'text': "<br />".join(other_team_strs)}],
            'title': ""
        }}

        if ts.debate_team.debate.round.is_break_round:
            cell = self._result_cell_class_four_elim(ts.win, cell)
            if ts.win is True:
                cell['text'] = _("advancing")
                cell['popover']['title'] = _("Advancing")
            elif ts.win is False:
                cell['text'] = _("eliminated")
                cell['popover']['title'] = _("Eliminated")
            else:
                cell['text'] = "–"
                cell['popover']['title'] = _("No result for debate")
        else:
            cell = self._result_cell_class_four(ts.points, cell)
            places = {0: _("4th"), 1: _("3rd"), 2: _("2nd"), 3: _("1st")}
            if ts.points is not None:
                place = places.get(ts.points, "??")
                cell['text'] = place
                cell['popover']['title'] = _("Took %(place)s") % {'place': place}
            else:
                cell['text'] = "–"
                cell['popover']['title'] = _("No result for debate")

        if show_score and ts.score is not None:
            cell['subtext'] = metricformat(ts.score)
            cell['popover']['content'].append(
                {'text': _("Received <strong>%s</strong> team points") % metricformat(ts.score)})

        if show_ballots:
            cell['popover']['content'].append(
                {'text': _("View debate ballot"), 'link': reverse_tournament('results-public-scoresheet-view',
                    self.tournament, kwargs={'pk': ts.debate_team.debate.id})})

        return cell

    def add_tournament_column(self, tournaments, key=ugettext_lazy("Tournament")):
        header = {
            'key': key, 'icon': 'tag', 'tooltip': _("Tournament")
        }
        data = [{
            'sort': t.seq, 'text': t.short_name, 'tooltip': t.short_name,
        } for t in tournaments]
        self.add_column(header, data)

    def add_round_column(self, rounds, key=ugettext_lazy("Round")):
        header = {
            'key': key, 'icon': 'clock', 'tooltip': _("Round")
        }
        data = [{
            'sort': round.seq, 'text': round.abbreviation, 'tooltip': round.name,
        } for round in rounds]
        self.add_column(header, data)

    def add_adjudicator_columns(self, adjudicators, hide_institution=False,
            hide_metadata=False, subtext=None):

        adj_data = []
        for adj in adjudicators:
            cell = {'text': adj.name}
            if self._show_record_links:
                cell['popover'] = {'content': [self._adjudicator_record_link(adj)]}
            if subtext is 'institution':
                cell['subtext'] = adj.institution.code
            adj_data.append(cell)
        self.add_column({'key': 'name', 'tooltip': _("Name"), 'icon': 'user'},
                        adj_data)

        if self.tournament.pref('show_adjudicator_institutions') and not hide_institution:
            self.add_column({
                'key': _("Institution"),
                'icon': 'home',
                'tooltip': _("Institution"),
            }, [adj.institution.code for adj in adjudicators])

        if not hide_metadata:
            adjcore_header = {
                'key': 'adjcore',
                'tooltip': _("Member of the Adjudication Core"),
                'icon': 'user-minus',
            }
            self.add_boolean_column(adjcore_header, [adj.adj_core for adj in adjudicators])

            independent_header = {
                'key': 'independent',
                'tooltip': _("Independent Adjudicator"),
                'icon': 'user-plus',
            }
            self.add_boolean_column(independent_header, [adj.independent for adj in adjudicators])

        if self.tournament.pref('show_unaccredited'):
            trainee_header = {
                'key': 'accredited',
                'tooltip': _("Always Trainee"),
                'icon': 'user-minus',
            }
            self.add_boolean_column(trainee_header, [adj.trainee for adj in adjudicators])

    def add_debate_adjudicators_column(self, debates, key=ugettext_lazy("Adjs"), show_splits=False, highlight_adj=None):
        da_data = []

        def construct_text(adjs_data):
            adjs_list = []
            for a in adjs_data:
                adj_str = a['adj'].name
                symbol = self.ADJ_SYMBOLS.get(a['position'])
                if symbol:
                    adj_str += " " + symbol
                if a.get('split', False):
                    adj_str += " <span class='text-danger'>💢</span>"
                if a['adj'] == highlight_adj:
                    adj_str = "<strong>" + adj_str + "</strong>"
                adjs_list.append(adj_str)
            return ', '.join(adjs_list)

        def construct_popover(adjs_data):
            popover_data = []
            for a in adjs_data:
                descriptors = []
                if a['position'] != AdjudicatorAllocation.POSITION_ONLY:
                    descriptors.append(self.ADJ_POSITION_NAMES[a['position']])
                descriptors.append("from %s" % a['adj'].institution.code)
                if a.get('split', False):
                    descriptors.append("<span class='text-danger'>in minority</span>")

                popover_data.append({'text': "%s (%s)" % (a['adj'].name, ", ".join(descriptors))})
                if self._show_record_links:
                    popover_data.append(self._adjudicator_record_link(a['adj']))

            return popover_data

        for debate in debates:
            adjs_data = []
            # The purpose of the second condition is to short-circuit debate.confirmed_ballot
            if show_splits and self.tournament.pref('ballots_per_debate') == 'per-adj' \
                    and debate.confirmed_ballot \
                    and debate.confirmed_ballot.result.is_voting \
                    and debate.confirmed_ballot.result.is_valid() \
                    and (self.admin or self.tournament.pref('show_splitting_adjudicators')):
                for adj, position, split in debate.confirmed_ballot.result.adjudicators_with_splits():
                    adjs_data.append({'adj': adj, 'position': position, 'split': bool(split)})
            else:
                for adj, position in debate.adjudicators.with_positions():
                    adjs_data.append({'adj': adj, 'position': position})

            if not debate.adjudicators.has_chair and debate.adjudicators.is_panel:
                adjs_data[0]['type'] = 'O'

            da_data.append({
                'text': construct_text(adjs_data),
                'popover': {
                    'title': _("Debate Adjudicators"),
                    'content' : construct_popover(adjs_data)
                }
            })

        self.add_column(key, da_data)

    def add_motion_column(self, motions, key=ugettext_lazy("Motion"), show_order=False):
        if show_order and self.tournament.pref('enable_motions'):
            self.add_column({
                'key': _("Order"),
                'icon': 'hash',
                'tooltip': _("Order as listed"),
            }, [{
                'text': motion.seq,
                'sort': motion.round.seq + (motion.seq * 0.1)
            } for motion in motions])

        motion_data = [{
            'text': motion.reference if motion.reference else '?',
            'popover': {'content' : [{'text': motion.text}]}
        } if motion else self.BLANK_TEXT for motion in motions]
        self.add_column(key, motion_data)

    def add_team_columns(self, teams, break_categories=False, hide_emoji=False,
                         show_divisions=True, hide_institution=False, key=None):

        team_data = [self._team_cell(team, hide_emoji=hide_emoji)
                     if not hasattr(team, 'anonymise') else self.BLANK_TEXT for team in teams]
        if key:
            header = {'key': key, 'text': key}
        else:
            header = {'key': 'team', 'tooltip': _("Team"), 'icon': 'users'}
        self.add_column(header, team_data)

        if break_categories:
            self.add_column(_("Categories"), [", ".join(bc.name for bc in team.break_categories) for team in teams])

        if self.tournament.pref('show_team_institutions') and not hide_institution:
            self.add_column({
                'key': _("Institution"),
                'icon': 'home',
                'tooltip': _("Institution"),
            }, [team.institution.code if not hasattr(team, 'anonymise') else self.BLANK_TEXT for team in teams])

        if show_divisions and self.tournament.pref('enable_divisions'):
            self.add_column({
                'key': _("Division"),
                'icon': 'layers',
                'tooltip': _("Division"),
            }, [team.division.name if team.division else self.BLANK_TEXT for team in teams])

    def add_speaker_columns(self, speakers):
        speaker_data = []
        for speaker in speakers:
            if getattr(speaker, 'anonymise', False):
                speaker_data.append("<em>" + _("Redacted") + "</em>")
            else:
                speaker_data.append(speaker.name)

        self.add_column({'key': 'name', 'tooltip': _("Name"), 'icon': 'user'},
                        speaker_data)

        speakercategory_set = self.tournament.speakercategory_set
        if not self.admin:
            speakercategory_set = speakercategory_set.filter(public=True)

        if speakercategory_set.exists():
            categories_data = []
            for speaker in speakers:
                category_strs = []
                for cat in speaker.categories.all():
                    if cat.public:
                        category_strs.append(cat.name)
                    elif self.admin:
                        category_strs.append("<em>" + cat.name + "</em>")
                categories_data.append(", ".join(category_strs))

            self.add_column({
                'key': _("Category"),
                'icon': 'user-check', # Not ideal but full name blows out tables
                'tooltip': _("Categories")
            }, categories_data)

    def add_debate_venue_columns(self, debates, with_times=True, for_admin=False):

        def construct_venue_cell(venue):
            if not venue:
                return {'text': ''}

            cell = {'text': venue.display_name, 'class': 'venue-name'}

            categories = venue.venuecategory_set.all()
            if not for_admin:
                # filter in Python, not SQL, because venuecategory_set should have been prefetched
                categories = [c for c in categories if c.display_in_public_tooltip]

            if len(categories) == 0:
                return cell

            descriptions = [category.description.strip() for category in categories]
            descriptions = ["<strong>" + description + "</strong>"
                    for description in descriptions if len(description) > 0]

            if len(descriptions) > 0:
                if len(descriptions) == 1:
                    categories_sentence = _("This venue %(predicate)s.") % {'predicate': descriptions[0]}
                else:
                    categories_sentence = _("This venue %(predicates)s, and %(last_predicate)s.") % {
                        'predicates': ", ".join(descriptions[:-1]),
                        'last_predicate': descriptions[-1]}

                cell['popover'] = {
                    'title': venue.display_name,
                    'content': [{'text': categories_sentence}]}

            return cell

        if self.tournament.pref('enable_divisions') and len(debates) > 0:
            # Add divisions immediately before the venue in in-rounds if enabled
            if debates[0].round.stage is debates[0].round.STAGE_PRELIMINARY:
                divisions_header = {
                    'key': _("Division"),
                    'icon': 'layers',
                    'tooltip': _("Division"),
                }
                divisions_data = ['D' + d.division.name if d.division else '' for d in debates]
                self.add_column(divisions_header, divisions_data)

        if self.tournament.pref('division_venues'):
            # For public displays of the draw fopr leagues we only
            # show the venue category derive from each debate's division
            venue_data = [{'text': d.division.venue_category if d.division else ''} for d in debates]
        else:
            venue_data = [construct_venue_cell(d.venue) for d in debates]

        venue_header = {
            'key': _("Venue"),
            'icon': 'map-pin',
            'tooltip': _("Venue"),
        }
        self.add_column(venue_header, venue_data)

        if with_times and self.tournament.pref('enable_debate_scheduling'):
            # If scheduling is enabled show date/times immediately after venues
            times_headers = [_("Date"), _("Time")]
            times_data = []
            for debate in debates:
                if debate.aff_team.type == Team.TYPE_BYE or debate.neg_team.type == Team.TYPE_BYE:
                    times_data.append(["", _("Bye")])
                elif debate.result_status == Debate.STATUS_POSTPONED:
                    times_data.append(["", _("Postponed")])
                elif debate.confirmed_ballot and debate.confirmed_ballot.forfeit:
                    times_data.append(["", _("Forfeit")])
                elif debate.time:
                    times_data.append([
                        formats.date_format(debate.time, "D jS F"),
                        formats.date_format(debate.time, "h:i A")])
                else:
                    times_data.append(["", ""])
            self.add_columns(times_headers, times_data)

    def add_draw_conflicts_columns(self, debates):
        venue_conflicts_by_debate = venue_conflicts_display(debates)  # dict of {debate: [conflicts]}
        adjudicator_conflicts_by_debate = adjudicator_conflicts_display(debates)  # dict of {debate: [conflicts]}

        conflicts_by_debate = []
        for debate in debates:
            # conflicts is a list of (level, message) tuples
            conflicts = [("primary", flag) for flag in debate.get_flags_display()]
            conflicts += [("primary", "%(team)s: %(flag)s" % {'team': debate.get_team(side).short_name, 'flag': flag})
                    for side in self.tournament.sides for flag in debate.get_dt(side).get_flags_display()]

            if self.tournament.pref('avoid_team_history'):
                history = debate.history
                if history > 0:
                    conflicts.append(("warning", ungettext("Teams have met once",
                            "Teams have met %(count)d times", history) % {'count': history}))

            if (self.tournament.pref('avoid_same_institution') and
                    len(set(t.institution_id for t in debate.teams)) != len(debate.teams)):
                conflicts.append(("warning", _("Teams are from the same institution")))

            conflicts.extend(adjudicator_conflicts_by_debate[debate])
            conflicts.extend(venue_conflicts_by_debate[debate])
            conflicts_by_debate.append(conflicts)

        conflicts_header = {'key': _("Conflicts/Flags")}
        conflicts_data = [{
            'text': "".join(["<div class=\"text-{0}\">{1}</div>".format(*conflict) for conflict in debate_conflicts]),
            'class': 'small'
        } for debate_conflicts in conflicts_by_debate]
        self.add_column(conflicts_header, conflicts_data)

        return adjudicator_conflicts_by_debate, venue_conflicts_by_debate

    def _standings_headers(self, info_list):
        headers = []
        for info in info_list:
            header = {
                'key': info['abbr'],
                'tooltip': force_text(info['name']).capitalize(),
                'icon': info['icon'],
                'text': force_text(info['abbr']) if info['icon'] is None else None
            }
            headers.append(header)
        return headers

    def add_ranking_columns(self, standings):
        headers = self._standings_headers(standings.rankings_info())
        data = []
        for standing in standings:
            data.append([{
                'text': rankingformat(ranking),
                'sort': ranking[0] or 99999,
            } for ranking in standing.iterrankings()])
        self.add_columns(headers, data)

    def add_metric_columns(self, standings):
        headers = self._standings_headers(standings.metrics_info())
        data = []
        for standing in standings:
            row = []
            for metric in standing.itermetrics():
                try:
                    sort = float(metric)
                except (TypeError, ValueError):
                    sort = 99999
                row.append({'text': metricformat(metric), 'sort': sort})
            data.append(row)
        self.add_columns(headers, data)

    def add_debate_ballot_link_column(self, debates):
        ballot_links_header = {'key': _("Ballot"), 'icon': 'search'}

        if self.admin:
            ballot_links_data = [{
                'text': _("View/Edit Ballot"),
                'link': reverse_tournament('results-ballotset-edit', self.tournament, kwargs={'pk': debate.confirmed_ballot.id})
            } if debate.confirmed_ballot else "" for debate in debates]
            self.add_column(ballot_links_header, ballot_links_data)

        elif self.tournament.pref('ballots_released'):
            ballot_links_header = {'key': _("Ballot"), 'icon': 'search'}
            ballot_links_data = []
            for debate in debates:
                if not debate.confirmed_ballot:
                    ballot_links_data.append("")
                elif self.tournament.pref('teams_in_debate') == 'bp' and debate.round.is_break_round:
                    ballot_links_data.append("")
                else:
                    ballot_links_data.append({
                        'text': _("View Ballot"),
                        'link': reverse_tournament('results-public-scoresheet-view', self.tournament, kwargs={'pk': debate.id})
                    })
            self.add_column(ballot_links_header, ballot_links_data)

    def add_debate_result_by_team_columns(self, teamscores):
        """Takes an iterable of TeamScore objects."""

        results_data = [self._result_cell(ts) for ts in teamscores]
        self.add_column(_("Result"), results_data)
        sides_data = [ts.debate_team.get_side_name().title()
            # Translators: "TBC" stands for "to be confirmed".
            if ts.debate_team.debate.sides_confirmed else _("TBC") for ts in teamscores]
        self.add_column(_("Side"), sides_data)

    def add_team_results_columns(self, teams, rounds):
        """ Takes an iterable of Teams, assumes their round_results match rounds"""
        for round_seq, round in enumerate(rounds):
            results = [self._result_cell(
                t.round_results[round_seq]) for t in teams]
            self.add_column(round.abbreviation, results)

    def add_debate_results_columns(self, debates):
        all_sides_confirmed = all(debate.sides_confirmed for debate in debates)  # should already be fetched
        side_abbrs = {side: get_side_name(self.tournament, side, 'abbr')
            for side in self.tournament.sides}

        results_data = []
        for debate in debates:
            row = []
            for side in self.tournament.sides:
                debateteam = debate.get_dt(side)
                team = debate.get_team(side)

                subtext = None if (all_sides_confirmed or not debate.sides_confirmed) else side_abbrs[side]
                cell = self._team_cell(team, hide_emoji=True, subtext=subtext)

                if self.tournament.pref('teams_in_debate') == 'two':
                    cell = self._result_cell_class_two(debateteam.win, cell)
                elif debate.round.is_break_round:
                    cell = self._result_cell_class_four_elim(debateteam.win, cell)
                else:
                    cell = self._result_cell_class_four(debateteam.points, cell)

                row.append(cell)
            results_data.append(row)

        if all_sides_confirmed:
            results_header = [get_side_name(self.tournament, side, 'abbr').capitalize()
                    for side in self.tournament.sides]
        else:
            results_header = [_("Team %(num)d") % {'num': i} for i in range(1, len(side_abbrs)+1)]

        self.add_columns(results_header, results_data)

    def add_standings_results_columns(self, standings, rounds, show_ballots):

        for round_seq, round in enumerate(rounds):
            results = [self._result_cell(
                s.round_results[round_seq],
                compress=True,
                show_score=True,
                show_ballots=show_ballots
            ) for s in standings]
            self.add_column(round.abbreviation, results)
