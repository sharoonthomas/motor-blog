"""Web frontend for motor-blog: actually show web pages to visitors
"""
import functools

import tornado.web
from tornado import gen
from tornado.options import options as opts
import motor
from werkzeug.contrib.atom import AtomFeed

from motor_blog.models import Post, Category
from motor_blog import cache
from motor_blog.text.link import absolute

# TODO: support HEAD

__all__ = (
    # Web
    'HomeHandler', 'PostHandler', 'MediaHandler', 'AllPostsHandler',
    'CategoryHandler',

    # Atom
    'FeedHandler',
)


# E.g. for Last-Modified header
HTTP_DATE_FMT = "%a, %d %b %Y %H:%M:%S GMT"

# TODO: document this as a means of refactoring
@cache.cached(key='categories', invalidate_event='categories_changed')
@gen.engine
def get_categories(db, callback):
    # This odd control flow ensures we don't confuse exceptions thrown
    # by find() with exceptions thrown by the callback
    category_docs = None
    try:
        category_docs = yield motor.Op(
            db.categories.find().sort('name').to_list)
    except Exception, e:
        callback(None, e)
        return

    callback(category_docs, None)

class MotorBlogHandler(tornado.web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super(MotorBlogHandler, self).__init__(*args, **kwargs)
        self.etag = None

    def _get_setting(self, setting_name):
        return self.application.settings[setting_name]

    def render(self, template_name, **kwargs):
        kwargs.setdefault('setting', self._get_setting)
        super(MotorBlogHandler, self).render(template_name, **kwargs)

    def get_current_user(self):
        """Logged-in username or None"""
        return self.get_secure_cookie('auth')

    def get_login_url(self):
        return self.reverse_url('login')

    def get_categories(self, callback):
        get_categories(self.settings['db'], callback=callback)

    def get_posts(self, *args, **kwargs):
        raise NotImplementedError()

    def compute_etag(self):
        # Set by check_etag decorator
        return self.etag


# TODO: ample documentation
def check_etag(get):
    @functools.wraps(get)
    @tornado.web.asynchronous
    @gen.engine
    def _get(self, *args, **kwargs):
        categorydocs = yield motor.Op(self.get_categories)
        self.categories = categories = [Category(**doc) for doc in categorydocs]

        postdocs = yield motor.Op(self.get_posts, *args, **kwargs)
        self.posts = posts = [Post(**doc) for doc in postdocs]

        mod = max(
            max(
                thing.date_created
                for things in (posts, categories)
                for thing in things
            ),
            max(post.mod for post in posts)
        )

        if not mod:
            # No posts or categories
            # TODO: if get() is not a generator?
            for i in get(self, *args, **kwargs):
                yield i
        else:
            last_modified = mod.strftime(HTTP_DATE_FMT)
            etag = str(last_modified)
            self.set_header('Last-Modified', last_modified)
            self.etag = etag

            inm = self.request.headers.get("If-None-Match")
            if inm and inm.find(etag) != -1:
                # No change since client's last request. Tornado will take care
                # of the rest.
                self.finish()

            else:
                # TODO: if get() is not a generator?
                for i in get(self, *args, **kwargs):
                    yield i

    return _get


class HomeHandler(MotorBlogHandler):
    def get_posts(self, callback, page_num=0):
        (self.settings['db'].posts.find(
                {'status': 'publish', 'type': 'post'},
                {'summary': False, 'original': False},
        ).sort([('_id', -1)])
        .skip(int(page_num) * 10)
        .limit(10)
        .to_list(callback))

    @tornado.web.addslash
    @check_etag
    def get(self, page_num=0):
        self.render('home.html',
            posts=self.posts, categories=self.categories,
            page_num=int(page_num))


class AllPostsHandler(MotorBlogHandler):
    def get_posts(self, callback):
        (self.settings['db'].posts.find(
                {'status': 'publish', 'type': 'post'},
                {'display': False, 'original': False},
        )
         .sort([('_id', -1)])
         .to_list(callback))

    @tornado.web.addslash
    @check_etag
    def get(self):
        self.render('all-posts.html',
            posts=self.posts, categories=self.categories)

#class AllPostsHandler(MotorBlogHandler):
#    @tornado.web.asynchronous
#    @gen.engine
#    @tornado.web.addslash
#    def get(self):
#        postdocs = yield motor.Op(
#            self.settings['db'].posts.find(
#                {'status': 'publish', 'type': 'post'},
#                {'display': False, 'original': False},
#            )
#            .sort([('_id', -1)])
#            .to_list)
#
#        posts = [Post(**postdoc) for postdoc in postdocs]
#        categories = yield motor.Op(get_categories, self.settings['db'])
#        categories = [Category(**doc) for doc in categories]
#
#        mod = max(
#            max(
#                thing.date_created
#                    for things in (posts, categories)
#                    for thing in things
#            ),
#            max(post.mod for post in posts)
#        )
#
#        self.etag = str(mod)
#        self.render(
#            'all-posts.html',
#            posts=posts, categories=categories)


class PostHandler(MotorBlogHandler):
    """Show a single blog post or page"""
    def get_posts(self, slug, callback):
        # TODO: for strict accuracy, the next / prev posts factor in to Etag
        # calculation
        slug = slug.rstrip('/')
        self.settings['db'].posts.find(
            {'slug': slug, 'status': 'publish'},
            {'summary': False, 'original': False}
        ).limit(-1).to_list(callback)

    @tornado.web.addslash
    @check_etag
    def get(self, slug):
        if not self.posts:
            raise tornado.web.HTTPError(404)

        post = self.posts[0]

        # Posts have previous / next navigation, but pages don't
        if post.type == 'post':
            prevdoc = yield motor.Op(
                self.settings['db'].posts.find({
                    'status': 'publish',
                    'type': 'post',
                    '_id': {'$lt': post.id}, # ids grow over time
                }).sort([('_id', -1)]).limit(-1).next)
            prev = Post(**prevdoc) if prevdoc else None

            nextdoc = yield motor.Op(
                self.settings['db'].posts.find({
                    'status': 'publish',
                    'type': 'post',
                    '_id': {'$gt': post.id}, # ids grow over time
                }).sort([('_id', 1)]).limit(-1).next)
            next = Post(**nextdoc) if nextdoc else None
        else:
            prev, next = None, None

        self.render(
            'single.html',
            post=post, prev=prev, next=next, categories=self.categories)


class CategoryHandler(MotorBlogHandler):
    """Page of posts for a category"""
    def get_posts(self, callback, slug, page_num=0):
        slug = slug.rstrip('/')
        self.settings['db'].posts.find({
            'status': 'publish',
            'type': 'post',
            'categories.slug': slug,
        }).sort([('_id', -1)]).limit(10).to_list(callback)

    @tornado.web.addslash
    @check_etag
    def get(self, slug, page_num=0):
        slug = slug.rstrip('/')
        for this_category in self.categories:
            if this_category.slug == slug:
                break
        else:
            raise tornado.web.HTTPError(404)

        self.render('category.html',
            posts=self.posts, categories=self.categories,
            this_category=this_category)


class MediaHandler(tornado.web.RequestHandler):
    """Retrieve media object, like an image"""
    @tornado.web.asynchronous
    @gen.engine
    def get(self, url):
        # TODO: great Etag and Last-Modified handling
        media = yield motor.Op(
            self.settings['db'].media.find_one, {'_id': url})

        if not media:
            raise tornado.web.HTTPError(404)

        self.set_header('Content-Type', media['type'])
        self.etag = media['mod'].strftime(HTTP_DATE_FMT)
        self.write(media['content'])
        self.finish()


class FeedHandler(MotorBlogHandler):
    def get_posts(self, callback, slug=None):
        query = {'status': 'publish', 'type': 'post'}

        if slug:
            slug = slug.rstrip('/')
            query['categories.slug'] = slug

        (self.settings['db'].posts.find(
            query,
            {'summary': False, 'original': False},
        ).sort([('_id', -1)])
        .limit(20)
        .to_list(callback))

    @check_etag
    def get(self, slug=None):
        if slug:
            slug = slug.rstrip('/')

        if not slug:
            this_category = None
        else:
            # Get all the categories and search for one with the right slug,
            # instead of actually querying for the right category, since
            # get_categories() is cached.
            slug = slug.rstrip('/')
            for this_category in self.categories:
                if this_category.slug == slug:
                    break
            else:
                raise tornado.web.HTTPError(404)

        title = opts.blog_name

        if this_category:
            title = '%s - Posts about %s' % (title, category.name)

        author = {'name': opts.author_display_name, 'email': opts.author_email}
        if this_category:
            feed_url = absolute(
                self.reverse_url('category-feed', this_category.slug))
        else:
            feed_url = absolute(self.reverse_url('feed'))

        updated = max(max(p.mod, p.date_created) for p in self.posts)

        feed = AtomFeed(
            title=title,
            feed_url=feed_url,
            url=absolute(self.reverse_url('home')),
            author=author,
            updated=updated,
            # TODO: customizable icon, also a 'logo' kwarg
            icon=absolute(self.reverse_url('theme-static', '/theme/static/square96.png')),
            generator=('Motor-Blog', 'https://github.com/ajdavis/motor-blog', '0.1'),
        )

        for post in self.posts:
            url = absolute(self.reverse_url('post', post.slug))
            feed.add(
                title=post.title,
                content=post.body,
                content_type='html',
                summary=post.summary,
                author=author,
                url=url,
                id=url,
                published=post.date_created,
                updated=post.mod)

        self.set_header('Content-Type', 'application/atom+xml; charset=UTF-8')
        self.write(unicode(feed))
        self.finish()
