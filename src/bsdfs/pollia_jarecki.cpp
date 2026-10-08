#include <mitsuba/core/properties.h>
#include <mitsuba/core/warp.h>
#include <mitsuba/render/bsdf.h>
#include <mitsuba/render/texture.h>
#include <mitsuba/render/pollia_jarecki.h>
#include <cmath>

NAMESPACE_BEGIN(mitsuba)

/**
 * PolRAY's Jarecki polarization transform with a separate Lambertian scalar
 * BRDF. This initial port uses constant Jarecki parameters per material.
 */
template <typename Float, typename Spectrum>
class PolliaJarecki final : public BSDF<Float, Spectrum> {
public:
    MI_IMPORT_BASE(BSDF, m_flags, m_components)
    MI_IMPORT_TYPES(Texture)

    PolliaJarecki(const Properties &props) : Base(props) {
        m_reflectance = props.get_texture<Texture>("reflectance", .5f);
        m_a = props.get<ScalarFloat>("a", .26f);
        m_b = props.get<ScalarFloat>("b", 2.f);
        m_n_real = props.get<ScalarFloat>("n_real", 1.26f);
        m_n_imag = props.get<ScalarFloat>("n_imag", .08f);
        m_xi0_max = props.get<ScalarFloat>("xi0_max", .26f);
        if (!std::isfinite(m_a) || !std::isfinite(m_b) ||
            !std::isfinite(m_n_real) || !std::isfinite(m_n_imag) ||
            !std::isfinite(m_xi0_max) || m_n_real <= 0.f ||
            m_n_imag < 0.f)
            Throw("pollia_jarecki: invalid material parameters");
        m_flags = BSDFFlags::DiffuseReflection | BSDFFlags::FrontSide;
        m_components.push_back(m_flags);
    }

    void traverse(TraversalCallback *cb) override {
        cb->put("reflectance", m_reflectance, ParamFlags::Differentiable);
    }

    std::pair<BSDFSample3f, Spectrum> sample(const BSDFContext &ctx,
                                             const SurfaceInteraction3f &si,
                                             Float /* sample1 */,
                                             const Point2f &sample2,
                                             Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFSample, active);
        BSDFSample3f bs = dr::zeros<BSDFSample3f>();
        active &= Frame3f::cos_theta(si.wi) > 0.f;
        if (unlikely(dr::none_or<false>(active) ||
                     !ctx.is_enabled(BSDFFlags::DiffuseReflection)))
            return { bs, 0.f };

        bs.wo = warp::square_to_cosine_hemisphere(sample2);
        bs.pdf = warp::square_to_cosine_hemisphere_pdf(bs.wo);
        bs.eta = 1.f;
        bs.sampled_type = +BSDFFlags::DiffuseReflection;
        bs.sampled_component = 0;
        Spectrum value = eval(ctx, si, bs.wo, active);
        return { bs, dr::select(active && bs.pdf > 0.f, value / bs.pdf, 0.f) };
    }

    Spectrum eval(const BSDFContext &ctx, const SurfaceInteraction3f &si,
                  const Vector3f &wo, Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFEvaluate, active);
        if (!ctx.is_enabled(BSDFFlags::DiffuseReflection))
            return 0.f;
        Float cos_i = Frame3f::cos_theta(si.wi),
              cos_o = Frame3f::cos_theta(wo);
        active &= cos_i > 0.f && cos_o > 0.f;
        UnpolarizedSpectrum scalar = m_reflectance->eval(si, active) *
                                     dr::InvPi<Float> * cos_o;

        if constexpr (is_polarized_v<Spectrum>) {
            // si.wi points toward the camera in radiance transport; wo points
            // toward the source. The helper uses that reverse-path ordering.
            Vector3f wi_light = ctx.mode == TransportMode::Radiance ? wo : si.wi;
            Vector3f wo_camera = ctx.mode == TransportMode::Radiance ? si.wi : wo;
            Vector3f n(0.f, 0.f, 1.f);
            pollia::JareckiParams<Float> jareckiParams {
                Float(m_a), Float(m_b), Float(m_n_real), Float(m_n_imag), Float(m_xi0_max) };
            auto local = pollia::evaluate_jarecki(wi_light, wo_camera, n, jareckiParams);
            auto j = pollia::world_jones(local, wi_light, wo_camera);
            auto m = pollia::mixture_mueller(j, local.gamma);
            Spectrum result = dr::zeros<Spectrum>();
            for (int i = 0; i < 4; ++i)
                for (int k = 0; k < 4; ++k)
                    result(i, k) = m(i, k) * scalar;
            return dr::select(active && local.valid, result, Spectrum(0.f));
        } else {
            return scalar & active;
        }
    }

    Float pdf(const BSDFContext &ctx, const SurfaceInteraction3f &si,
              const Vector3f &wo, Mask active) const override {
        MI_MASKED_FUNCTION(ProfilerPhase::BSDFEvaluate, active);
        if (!ctx.is_enabled(BSDFFlags::DiffuseReflection))
            return 0.f;
        active &= Frame3f::cos_theta(si.wi) > 0.f && Frame3f::cos_theta(wo) > 0.f;
        return dr::select(active, warp::square_to_cosine_hemisphere_pdf(wo), 0.f);
    }

    std::pair<Spectrum, Float> eval_pdf(const BSDFContext &ctx,
                                        const SurfaceInteraction3f &si,
                                        const Vector3f &wo,
                                        Mask active) const override {
        return { eval(ctx, si, wo, active), pdf(ctx, si, wo, active) };
    }

    Mask has_attribute(const std::string &name, Mask active) const override {
        if (name == "pollia_jarecki_params" || name == "pollia_optical_constants" ||
            name == "pollia_reflectance")
            return active;
        return Base::has_attribute(name, active);
    }

    UnpolarizedSpectrum eval_attribute(const std::string &name,
                                        const SurfaceInteraction3f &si,
                                        Mask active) const override {
        if (name == "pollia_reflectance")
            return m_reflectance->eval(si, active);
        return Base::eval_attribute(name, si, active);
    }

    Float eval_attribute_1(const std::string &name,
                           const SurfaceInteraction3f &si,
                           Mask active) const override {
        if (name == "pollia_reflectance")
            return m_reflectance->eval_1(si, active);
        return Base::eval_attribute_1(name, si, active);
    }

    Color3f eval_attribute_3(const std::string &name,
                             const SurfaceInteraction3f &si,
                             Mask active) const override {
        if (name == "pollia_jarecki_params")
            return Color3f(Float(m_a), Float(m_b), Float(m_xi0_max)) & active;
        if (name == "pollia_optical_constants")
            return Color3f(Float(m_n_real), Float(m_n_imag), Float(0.f)) & active;
        return Base::eval_attribute_3(name, si, active);
    }

    BSDFFeatures3f eval_features(const SurfaceInteraction3f &si,
                                 Mask active) const override {
        return { m_reflectance->eval(si, active), 0.f, 0.f, si.sh_frame, 1.f };
    }

    std::string to_string() const override {
        std::ostringstream oss;
        oss << "PolliaJarecki[reflectance=" << string::indent(m_reflectance)
            << ", a=" << m_a << ", b=" << m_b << ", n=(" << m_n_real
            << ", " << m_n_imag << "), xi0_max=" << m_xi0_max << "]";
        return oss.str();
    }

    MI_DECLARE_CLASS(PolliaJarecki)
private:
    ref<Texture> m_reflectance;
    ScalarFloat m_a, m_b, m_n_real, m_n_imag, m_xi0_max;
    MI_TRAVERSE_CB(Base, m_reflectance)
};

MI_EXPORT_PLUGIN(PolliaJarecki)
NAMESPACE_END(mitsuba)
