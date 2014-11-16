from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import ugettext as _

from evap.evaluation.models import Course, UserProfile
from evap.evaluation.tools import is_external_email

import xlrd
from collections import OrderedDict, defaultdict

# taken from https://stackoverflow.com/questions/390250/elegant-ways-to-support-equivalence-equality-in-python-classes
class CommonEqualityMixin(object):

    def __eq__(self, other):
        return (isinstance(other, self.__class__)
            and self.__dict__ == other.__dict__)



class UserData(CommonEqualityMixin):
    """Holds information about a user, retrieved from the Excel file."""

    def __init__(self, username, first_name, last_name, title, email, is_responsible):
        self.username = username.strip().lower()
        self.first_name = first_name.strip()
        self.last_name = last_name.strip()
        self.title = title.strip()
        self.email = email.strip().lower()
        self.is_responsible = is_responsible

    def store_in_database(self):
        user, created = User.objects.get_or_create(username=self.username)
        user.first_name = self.first_name
        user.last_name = self.last_name
        user.email = self.email
        user.save()

        profile = UserProfile.get_for_user(user=user)
        profile.title = self.title
        profile.is_external = is_external_email(self.email)
        if profile.needs_login_key:
            profile.refresh_login_key()
        profile.save()
        return created
        

class CourseData(CommonEqualityMixin):
    """Holds information about a course, retrieved from the Excel file."""

    def __init__(self, name_de, name_en, kind, degree, responsible_email):
        self.name_de = name_de.strip()
        self.name_en = name_en.strip()
        self.kind = kind.strip()
        self.degree = degree.strip()
        self.responsible_email = responsible_email

    def store_in_database(self, vote_start_date, vote_end_date, semester):
        course = Course(name_de=self.name_de,
                        name_en=self.name_en,
                        kind=self.kind,
                        vote_start_date=vote_start_date,
                        vote_end_date=vote_end_date,
                        semester=semester,
                        degree=self.degree)
        course.save()
        responsible_dbobj = User.objects.get(email=self.responsible_email)
        course.contributions.create(contributor=responsible_dbobj, course=course, responsible=True, can_edit=True)


class ExcelImporter(object):
    def __init__(self, request):
        self.associations = OrderedDict()
        self.request = request
        self.book = None
        self.skip_first_n_rows = 1 # first line contains the header
        self.errors = []

    def read_book(self, excel_file):
        self.book = xlrd.open_workbook(file_contents=excel_file.read())

    def check_column_count(self, expected_column_count):
        for sheet in self.book.sheets():
            if (sheet.ncols != expected_column_count):
                self.errors.append(_(u"Wrong number of columns in sheet '{}'. Expected: {}, actual: {}").format(sheet.name, expected_column_count, sheet.ncols))

    def for_each_row_in_excel_file_do(self, parse_row_function):
        for sheet in self.book.sheets():
            try:
                for row in range(self.skip_first_n_rows, sheet.nrows):
                    line_data = parse_row_function(sheet.row_values(row), sheet.name, row)
                    # store data objects together with the data source location for problem tracking
                    self.associations[(sheet.name, row)] = line_data

                messages.success(self.request, _(u"Successfully read sheet '%s'.") % sheet.name)
            except Exception:
                messages.warning(self.request, _(u"A problem occured while reading sheet '%s'.") % sheet.name)
                raise
        messages.success(self.request, _(u"Successfully read excel file."))


    def generate_external_usernames_if_external(self, user_data_list):
        for user_data in user_data_list:
            if is_external_email(user_data.email):
                if user_data.username != "":
                    self.errors.append(_('User {}: Username must be empty for external users.').format(user_data.username))
                user_data.username = (user_data.first_name + '.' + user_data.last_name + '.ext').lower()

    def show_errors(self):
        for error in self.errors:
            messages.error(self.request, error)


class EnrolmentImporter(ExcelImporter):
    def __init__(self, request):
        super(EnrolmentImporter, self).__init__(request)
        self.consolidated_data = None

    def read_one_enrollment(self, data, sheet_name, row_id):
        student_data = UserData(username=data[3], first_name=data[2], last_name=data[1], email=data[4], title='', is_responsible=False)
        responsible_data = UserData(username=data[11], first_name=data[10], last_name=data[9], title=data[8], email=data[12], is_responsible=True)
        course_data = CourseData(name_de=data[6], name_en=data[7], kind=data[5], degree=data[0], responsible_email=responsible_data.email)
        return (student_data, responsible_data, course_data)

    def process_user(self, dictionary, user_data, sheet, row):
        curr_email = user_data.email
        if curr_email not in dictionary:
            dictionary[curr_email] = user_data
        else:
            if not user_data == dictionary[curr_email]:
                self.errors.append(_(u'Sheet "{}", row {}: The users\'s data (email: {}) differs from it\'s data in a previous row.').format(sheet, row, curr_email))

    def process_course(self, dictionary, course_data, sheet, row):
        course_id = (course_data.degree, course_data.name_en) 
        if course_id not in dictionary:
            dictionary[course_id] = course_data
        else:
            if not course_data == dictionary[course_id]:
                self.errors.append(_(u'Sheet "{}", row {}: The course\'s "{}" data differs from it\'s data in a previous row.').format(sheet, row, course_data.name_en))

    def consolidate_enrolment_data(self):
        # these are dictionaries to not let this become O(n^2)
        users = {}
        courses = {}
        enrolments = []
        degrees = set()
        for (sheet, row), (student_data, responsible_data, course_data) in self.associations.items():
            self.process_user(users, student_data, sheet, row)
            self.process_user(users, responsible_data, sheet, row)
            self.process_course(courses, course_data, sheet, row)
            enrolments.append((course_data, student_data))
            degrees.add(course_data.degree)
        self.consolidated_data = (users, courses, enrolments, degrees)

    def check_enrolment_data_correctness(self):
        users, courses, enrolments, degrees = self.consolidated_data

        for user_data in users.values():
            if not is_external_email(user_data.email) and user_data.username == "":
                self.errors.append(_('Emailaddress {}: Username cannot be empty for non-external users.').format(user_data.email))
            if not is_external_email(user_data.email) and len(user_data.username) > 20 :
                self.errors.append(_('User {}: Username cannot be longer than 20 characters for non-external users.').format(user_data.email))
            if user_data.first_name == "":
                self.errors.append(_('User {}: First name is missing.').format(user_data.email))
            if user_data.last_name == "":
                self.errors.append(_('User {}: Last name is missing.').format(user_data.email))
            if user_data.email == "":
                self.errors.append(_('User {}: Email address is missing.').format(user_data.email))

    def check_enrolment_data_sanity(self):
        pass

    def write_enrolments_to_db(self, semester, vote_start_date, vote_end_date):
        users, courses, enrolments, degrees = self.consolidated_data
        students_created = 0
        responsibles_created = 0

        with transaction.atomic():
            for user_data in users.values():
                created = user_data.store_in_database()
                if created:
                    if user_data.is_responsible:
                        responsibles_created += 1
                    else:
                        students_created += 1
            for course_data in courses.values():
                course_data.store_in_database(vote_start_date, vote_end_date, semester)

            for course_data, student_data in enrolments:
                course = Course.objects.get(semester=semester, name_de=course_data.name_de, degree=course_data.degree)
                student = User.objects.get(email=student_data.email)
                course.participants.add(student)
                
        messages.success(self.request, _("Successfully created %(courses)d course(s), %(students)d student(s) and %(responsibles)d contributor(s).") % dict(courses=len(courses), students=students_created, responsibles=responsibles_created))

    @classmethod
    def process(cls, request, excel_file, semester, vote_start_date, vote_end_date):
        """Entry point for the view."""
        try:
            importer = cls(request)
            importer.read_book(excel_file)
            importer.check_column_count(13)
            if importer.errors:
                importer.show_errors()
                messages.error(importer.request, _("The input data is malformed. No data was imported."))
                return
            importer.for_each_row_in_excel_file_do(importer.read_one_enrollment)
            importer.consolidate_enrolment_data()
            importer.generate_external_usernames_if_external(importer.consolidated_data[0].values())
            importer.check_enrolment_data_correctness()
            importer.check_enrolment_data_sanity()
            if importer.errors:
                importer.show_errors()
                messages.error(importer.request, _("Errors occurred while parsing the input data. No data was imported."))
                return
            importer.write_enrolments_to_db(semester, vote_start_date, vote_end_date)
        except Exception as e:
            raise
            messages.error(request, _(u"Import finally aborted after exception: '%s'" % e))
            if settings.DEBUG:
                # re-raise error for further introspection if in debug mode
                raise


class UserImporter(ExcelImporter):
    def __init__(self, request):
        super(UserImporter, self).__init__(request)

    def read_one_user(self, data, sheet_name, row_id):
        user_data = UserData(username=data[0], title=data[1], first_name=data[2], last_name=data[3], email=data[4])
        return (user_data)

    def save_users_to_db(self):
        """Stores the read data in the database. Errors might still
        occur because of the data already in the database."""

        with transaction.atomic():
            users_count = 0
            for (sheet, row), (user_data) in self.associations.items():
                try:
                    created = user_data.store_in_database()
                    if created:
                        users_count += 1

                except Exception as e:
                    messages.error(self.request, _("A problem occured while writing the entries to the database. The original data location was row %(row)d of sheet '%(sheet)s'. The error message has been: '%(error)s'") % dict(row=row, sheet=sheet, error=e))
                    raise
        messages.success(self.request, _("Successfully created %(users)d user(s).") % dict(users=users_count))

    @classmethod
    def process(cls, request, excel_file):
        """Entry point for the view."""
        try:
            importer = cls(request)
            importer.read_book(excel_file)
            importer.check_column_count(5)
            if importer.errors:
                importer.show_errors()
                messages.error(importer.request, _("The input data is malformed. No data was imported."))
                return
            importer.for_each_row_in_excel_file_do(importer.read_one_user)
            importer.generate_external_usernames_if_external(self.associations.values())
            if importer.errors:
                importer.show_errors()
                messages.error(importer.request, _("Errors occurred while parsing the input data. No data was imported."))
                return
            importer.save_users_to_db()
        except Exception as e:
            raise
            messages.error(request, _(u"Import finally aborted after exception: '%s'" % e))
            if settings.DEBUG:
                # re-raise error for further introspection if in debug mode
                raise
